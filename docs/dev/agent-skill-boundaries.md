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
| `issue-reviewer`（loop worker SubAgent） | read-only | `dontAsk` | Bash, Read, Grep, Glob | Agent, Edit, Write, MultiEdit, Skill |
| `issue-author` | write | `acceptEdits` | Bash, Read, Write | Agent, Edit, MultiEdit |
| `implementation-worker` | write | `acceptEdits` | Read, Grep, Glob, Bash, Edit, Write, MultiEdit | — |
| `post-merge-cleanup-worker` | cleanup | `default` | Bash, Read | Agent, Edit, Write, MultiEdit |

## Codex Dispatch Guardrail（Codex ディスパッチ境界）

- Codex CLI の root thread は control-plane 専用とし、`implementation-worker` / `test-runner` / `pr-reviewer` / `post-merge-cleanup-worker` を明示 spawn して data-plane を委譲する
- repo-side deterministic guardrail の canonical evidence は event-derived `SUBAGENT_LAUNCH_LEDGER_V1` とし、worker self-report 単独では spawn evidence とみなさない
- `SUBAGENT_LAUNCH_LEDGER_V1.coverage_scope` は support 済みの `SubagentStart` / `PreToolUse(Bash|apply_patch|Edit|Write)` 観測範囲を明示する。未対応 path の absence を「完全防止」の証拠として主張しない
- project-local `.codex/config.toml` は profile routing の証拠として扱わず、actual runtime contract と launch-ledger evidence を validator 対象にする
- live spawn の runtime verification は `#601` に deferred し、この文書で扱うのは evidence 不足時に fail-closed する repo-side 監査境界のみ

## Manual Codex Spark Agents（手動 Codex Spark エージェント）

`spark-skim` / `spark-worker` / `spark-deep` は manual invocation only の Codex custom subagent として扱う。human が agent 名を明示 spawn した場合だけ使い、workflow root の auto dispatch 先にしない。

### Reasoning routing policy（推論ルーティング方針）

| Agent | Permissions | 想定用途 | 補足 |
|---|---|---|---|
| `spark-skim` | `loop-protocol-readonly` | 軽量な read-only triage / 要点抽出 | low reasoning |
| `spark-worker` | `loop-protocol-rtk` | Allowed Paths 内の manual bounded edit helper | medium reasoning。`implementation-worker` / `issue-author` / `pr-reviewer` の代替ではない |
| `spark-deep` | `loop-protocol-readonly` | 複雑な read-only analysis / risk surfacing | high reasoning |

- `xhigh` reasoning は今回採用しない。account plan / rollout 差分や運用実績を踏まえた follow-up 判断対象とする。
- heavy agent 非置換を原則とし、既存 `implementation-worker` / `issue-author` / `pr-reviewer` / `test-runner` の routing は維持する。
- `spark-worker` は manual bounded edit helper であり、Issue authoring、PR review judgment、loop orchestration、publish 操作を担当しない。
- `HOOK_COMMAND_REPAIR_HINT_V1` は agent steering 用の bounded diagnostics であり、rules / hooks の authorization を上書きしない。
- `HOOK_COMMAND_REPAIR_HINT_V1` が `suggested_command` を返しても、それは `direct rtk git ...` の exact / bounded repair 候補に限る。wrapper 展開や bypass shell を agent が自動採用してはならない。
- `allowed_paths_missing_for_git_mutation` や `issue_context_required` は runtime contract 未解決の停止信号として扱い、agent は publish を続行せず binding/source-of-truth を確認する。
- publish retry は branch 名一致だけで green 扱いせず、`PUBLISH_LANE_DECISION_V1` の `expected_remote_head` / `current_remote_head` / `local_head` / `verified_head` / `declared_publish_head` / `allowed_paths_gate_status` / `remote_readback_source` / `decision_inputs_complete` を read-only preflight で揃えてから判断する。
- `PUBLISH_SAFETY_STOP_REPORT_V1` の `reason_code` は少なくとも `branch_mismatch` / `stale_remote_head` / `local_head_mismatch` / `remote_fast_forward_by_same_scope` / `remote_head_scope_contamination` / `allowed_paths_gate_not_ok` / `publish_guard_context_missing` / `publish_guard_context_invalid` を使い、manual remote update への暗黙フォールバックを禁止する。

### Prompt examples（プロンプト例）

- `spark-skim`: `Spawn spark-skim and read only docs/dev/agent-skill-boundaries.md plus .codex/agents. Return SPARK_AGENT_RESULT_V1 with evidence refs only.`。軽量調査だけを依頼する例。
- `spark-worker`: `Spawn spark-worker for a bounded edit inside the listed Allowed Paths only. Do not review the PR or publish anything.`。編集範囲を厳密に縛る例。
- `spark-deep`: `Spawn spark-deep for read-only analysis of validator / fixture drift and return risk flags without editing files.`。深い read-only 分析を依頼する例。

### Positive smoke and negative smoke policy（ポジティブ/ネガティブ smoke 方針）

- positive smoke: human が `spark-skim` / `spark-worker` / `spark-deep` をそれぞれ明示 spawn し、`SUBAGENT_LAUNCH_LEDGER_V1.launches[]` に対象 agent 名、runtime model、reasoning effort、default permissions が記録されることを確認する。
- negative smoke: `issue-refinement-loop` / `impl-review-loop` の通常ルート実行後に ledger を確認し、`spark-*` が auto dispatch されていないことを確認する。routing 変更は別 Issue とする。
- auto dispatch を有効化する変更は本スコープ外であり、planner / loop routing / workflow root の変更を伴う別 implementation issue で扱う。

### Manual smoke evidence minimum（手動 smoke の最小証跡）

- `Codex CLI version`。Codex CLI の版情報。
- `install route`。導入経路の記録。
- `auth mode category`。認証モード区分。
- `agent prompt`。実際に使った agent 指示。
- `ledger path`。ledger artifact の保存先。
- `Known limitation`。既知制約の明記。

- manual smoke evidence には secret、raw transcript、raw logs を残さない。必要なら構造化 summary と ledger path のみを残す。
- Spark 3 agent は Codex-only parity exception として扱い、fixture では `parity_mode: codex_only`、`parity_exception_reason`、`claude_agent_path: null` を保持する。

### `review-issue` / `issue-reviewer` の使い分け

| エントリ | 種別 | 呼び出し元 | 役割 |
|---|---|---|---|
| `review-issue` Skill | Skill（手順書） | main session・各 SubAgent | Issue 本文の品質を決定論的チェックして `REVIEW_ISSUE_RESULT_V1` を返す手順。結果 schema を返す。 |
| `review-issue` SubAgent（`review-issue.md`） | write SubAgent | main session（standalone） | Issue 本文編集を伴う standalone レビュー。`gh issue edit` を human-in-the-loop で実行する。 |
| `issue-reviewer` SubAgent（`issue-reviewer.md`） | read-only SubAgent | `issue-refinement-loop`（loop worker） | `review-issue` skill を内部で実行し `REVIEW_ISSUE_RESULT_V1` を返すのみ。Issue の mutation を行わない。 |

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
| `edit-issue` | 既存 Issue 本文更新の transaction 手順（candidate body / guard / readiness / controlled executor / bounded result）| `issue-author` SubAgent、`issue-refinement-loop`、`post-merge-cleanup`、`review-issue`（needs-fix 適用時） |
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
| `codebase-investigator` | 大規模コードベース調査の役割（常に `gemini-cli-headless-delegation` skill に委譲して大規模文脈読み取りを行う）。file evidence の精度保証は `.claude/skills/gemini-cli-headless-delegation/references/usage-contract.md#REPO_EVIDENCE_REF_V1` に SSOT 化される。証拠参照の正本をここへ寄せる。 |

| Skill | 手順 |
|---|---|
| `ssot-discovery` | `docs/` 配下を SSOT として横断探索する手順 |
| `gemini-cli-headless-delegation` | Gemini CLI への headless 委譲手順。file evidence の structure (REPO_EVIDENCE_REF_V1) と verification contract が定義されている。検証契約の置き場。 |
| `nlm-skill` | NotebookLM CLI / MCP 操作（既存導入） |

## Repository Folder Policy Change Route（リポジトリフォルダポリシー変更ルート）

- repo-approved temporary workspace の正本は `docs/dev/repository-folder-policy.md` とする。
- `.tmp/` / `.temp/` / `.tmp-*` の扱いを変える変更は、hook advisory・schema・tests・cleanup docs を同一 PR で更新する。
- `tmp/`、`.claude/tmp/`、`.claude/worktrees/` の cleanup authority を変える場合は、folder policy SSOT と `post-merge-cleanup` の safety rules を同時に更新する。
- root temporary residue policy は advisory hook と operator docs まではこの boundary で扱うが、cleanup classifier の専用 output field や ownership marker executor 実装まで同時に存在するとは限らない。PR / review では未実装部分を `Not controlled` として扱う。


## gh CLI コマンド 5 分類語彙（Issue #1124）

`local_main_branch_guard.py` は gh issue/pr コマンドを以下の 5 分類で判定する。
この語彙は `hook-boundaries.md` と本ドキュメントで統一する。

| 分類 | reason_code | 代表コマンド |
|---|---|---|
| `display_readonly_command` | `readonly_command` | `gh issue view`, `gh pr view`, `gh issue list` |
| `readonly_artifact_export_command` | `readonly_command` | `gh issue view <N> ... > tmp/<file>` |
| `github_issue_mutation_command` | `github_remote_ops_command` | `gh issue create`（--repo + --body-file 必須） |
| `github_pr_metadata_command` | `github_remote_ops_command` | `gh pr comment/edit`（post-merge-cleanup 最小集合） |
| `github_destructive_command` | `gh_mutation_denied` | `gh pr merge`, `gh pr checkout`, `gh pr update-branch` |

### github_issue_mutation_command の allow 条件

managed skill（`create-issue`）が `gh issue create` を実行する際:
- `--repo squne121/loop-protocol` 必須（完全一致）
- `--body-file tmp/<path>` 必須（tmp/ 相対パス、`-` は不可）
- `--title <value>` 必須
- interactive フラグ（`--editor` / `-e` / `--web` / `-w`）は block
raw `gh issue edit` / `gh issue comment` は `gh_mutation_denied` でブロックされる。
`gh api -f body=...` / `gh api graphql -f query='mutation { ... }'` /
`gh api --method POST ...` のような allowlist 外 `gh api` は
`github_api_command` (`gh_api_not_allowed`) でブロックされる。
これらの条件を満たさない `gh issue create` も `gh_mutation_denied` でブロックされる。

## issue-refinement-loop Producer / Publisher 責務分割（#1154 / #1165 / #1166、producer/publisher の責務境界）

`issue-refinement-loop` の producer / publisher 系は以下の 3 Issue で責務を分割する。
各 Issue はそれぞれ独立した scope を持ち、相互に依存しない fail-closed routing を実装する。

| Issue | 責務 | 対象コンポーネント |
|---|---|---|
| #1154 | root checkout からの `preflight.run` 専用 skill runtime executor | `skill_runtime_exec.py`、`SKILL_RUNTIME_COMMAND_POLICY_V2` |
| #1165 | producer / compact producer の fail-closed routing（schema mismatch / output budget / termination bypass 阻止） | `compact_review_result.py`、`compact_author_result.py`、`run_refinement_preflight.py` |
| #1166 | `publish_termination_report.py` の controlled mutation policy（publishable=true 時のみ gh comment） | `publish_termination_report.py`、`render_termination_report.py` |

### 責務境界の明文化

- `#1154` のみが `preflight.run` の root allow / privileged executor を実装する。`gh.*`、`publish_termination_report.py`、producer output budget / schema mismatch は #1154 boundary の対象外。
- `#1165` は producer の fail-closed routing を実装する。publish_termination_report.py の呼び出しは一切行わない。schema mismatch / output budget violation 時は canonical failure envelope（STATUS/NEXT_ACTION/REASON_CODE/ARTIFACT/ARTIFACT_SHA256）を stdout に出力し、原文（raw issue body / comment / diff）を stdout に含まない。
- `#1166` は publish_termination_report.py の controlled mutation policy を実装する。publishable=true の場合のみ gh issue comment を呼び出す。producer failure はこの policy の対象外。

### fail-closed routing 不変条件

- producer failure（schema mismatch / output budget violation）のいずれでも `publish_termination_report.publish()`、`_post_github_comment()` は呼ばれない
- compact producer の stdout は常に 2048 UTF-8 bytes 以下
- raw issue body / raw comment / raw diff は compact producer の stdout に含まない

## Privileged Skill Runtime Command Boundary（特権 skill runtime コマンド境界）

Issue #1154 では、root checkout からの skill runtime command を registry 全件へ一般化せず、`preflight.run` だけを deny-by-default で許可する。

```yaml
SKILL_RUNTIME_COMMAND_POLICY_V2:
  eligible_command_ids:
    preflight.run:
      execution_class: exact_skill_runtime
      required_cwd: canonical_main_root
      required_branch: default_branch
      allowed_write_roots:
        - .claude/artifacts/issue-refinement-loop/{active_issue}/
      network_effect: github_read_only
    preflight.run.with_anchor:
      execution_class: exact_skill_runtime_anchor
      required_cwd: canonical_main_root
      required_branch: default_branch
      allowed_write_roots:
        - .claude/artifacts/issue-refinement-loop/{active_issue}/
      network_effect: github_read_only
```

- root checkout から許可される command class は `uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number <active> --repo squne121/loop-protocol` の exact form のみ
- Issue #1498 で `preflight.run.with_anchor`（sibling exact profile）を追加した。`preflight.run` の argv / placeholders / execution_class / timeout は一切変更していない。`preflight.run.with_anchor` は末尾に `--anchor-comment-url <canonical GitHub issue comment URL>` を持つ 12-token exact form のみを受理し、URL は `https://github.com/<owner>/<repo>/issues/<N>#issuecomment-<M>` の canonical shape（percent-encoding・query・userinfo・port・非 GitHub host・`/pull/` パス・`discussion_r` fragment を拒否）に限定され、URL 内の owner/repo/issue 番号は CLI の `--repo` / `--issue-number` に context-binding される
- `mutation: false`、`cwd_policy: repo_root`、registry 登録済みであること自体は認可根拠に使わない
- `preflight.run` の root no-worktree profile では issue source は exact argv の `--issue-number` とし、active issue worktree と `LOOP_ISSUE_NUMBER` は不要
- `preflight.run` 以外の privileged command は従来どおり active issue worktree が `git worktree list --porcelain -z` catalog で一意に解決できる entry のみを許可し、`.claude/worktrees/issue-<N>-*` の stale directory prefix match を認可根拠に使わない
- executor は `preflight.run` では `repo` と exact argv の `issue_number` に束縛し、その他 command は active worktree context に束縛する。registry は canonical path から load する
- canonical repo binding は `https://github.com/<owner>/<repo>(.git)` と `git@github.com:<owner>/<repo>(.git)` の strict parser だけを受理する
- executor は trusted PATH から解決した `uv` / `python3` を使い、allowlist env と filesystem snapshot/postcondition で ignored/transient outside write と PATH poisoning を fail-close する
- postcondition が fail-close するのは OS-level filesystem sandbox ではなく、**repo tree outside allowed artifact root** への変更である
- `uv.pytest`、`pnpm.typecheck`、`pnpm.lint`、`pnpm.test`、`pnpm.build`、`gh.*`、未登録 script は root allow 対象外
- `publish_termination_report.py` と producer output budget / schema mismatch / termination bypass は本 boundary の対象外

## ci-test-performance Consumer Routing（consumer ルーティング）

`ci-test-performance` Skill は CI テストパフォーマンスのレーン分類・hotspot 分析・意思決定を担う。
詳細な手順は `.claude/skills/ci-test-performance/SKILL.md` を正本とする。
以下は設計上の consumer routing 定義であり、各 agent/skill ファイルの実更新は別 follow-up Issue で対応する。

### Consumer 一覧

| Consumer | 役割 | 読むタイミング | 実更新状況 |
|---|---|---|---|
| `issue-contract-review` | CI 関連 Issue で `ci-test-performance` が Required Skills に欠落、または CI runtime evidence plan が存在しない場合に `blocked` を返すことができる | CI 関連 Issue の contract review 時 | 実更新は follow-up Issue に分離（docs-only 定義） |
| `implementation-worker` | `.github/workflows/**`・`pyproject.toml`・`uv.lock`・pytest/Ruff/xdist/CI artifact 関連を触る前に `ci-test-performance` を読む。PR 本文または artifact に `CI_TEST_PERFORMANCE_DECISION_V1` を残す | CI 関連 path 編集前 | Codex CLI 側は `.codex/agents/implementation-worker.toml` に routing 追加済み。Claude Code 側実更新は follow-up Issue に分離 |
| `test-runner` | VC 実行・runtime artifact 確認の担当。lane 設計の意思決定者ではなく、PASS/FAIL/SKIP 分類と artifact 整合性チェックを行う | VC 実行時 | Codex CLI 側は `.codex/agents/test-runner.toml` に routing 追加済み。Claude Code 側実更新は follow-up Issue に分離 |
| `pr-reviewer` | CI 関連 PR で `CI_TEST_PERFORMANCE_DECISION_V1`・lane 判断・`ci_runtime_baseline` 比較・self-report 以外の証跡が存在しない場合は `REQUEST_CHANGES` を返すことができる | CI 関連 PR review 時 | Codex CLI 側は `.codex/agents/pr-reviewer.toml` に routing 追加済み。Claude Code 側実更新は follow-up Issue に分離 |

### Claude Code 側 consumer 更新の扱い

以下の Claude Code 側ファイルは本 Issue (#1060) のスコープ外とし、別 follow-up Issue で対応する:

- `.claude/agents/implementation-worker.md` — CI 関連 path 編集時に `ci-test-performance` を読む routing
- `.claude/agents/pr-reviewer.md` — CI 関連 PR review で `CI_TEST_PERFORMANCE_DECISION_V1` 確認の routing
- `.claude/agents/test-runner.md` — VC 実行時の artifact 整合性確認 routing
- `.claude/skills/issue-contract-review/SKILL.md` — CI 関連 Issue の preflight gate への組み込み

本 Issue では「どの consumer が、いつ、どのような判断をするか」の設計上の routing 定義のみを本セクションに記載する。

### hook による advisory suggestion

hook 実装（CI 関連 path を edit した際に `ci-test-performance` を読むよう `additionalContext` を出す）は本 Issue スコープ外。
Claude Code の `FileChanged` / `PreToolUse` hook と Codex CLI の `PreToolUse` hook のどちらで実装するかも含めて別 follow-up Issue で決定する。

## Spec Kit (speckit-*) スキル責務境界

specify-cli v0.8.13 upstream から取得した 9 本の speckit-* スキルを `.claude/skills/` に配置する（Issue #303）。
upstream 名をそのまま採用（ADR 0002 確定方針 — `upstream_name_adopted`）。

### スキル一覧・役割・loading tier

| Skill | 行数 | 役割 | Loading Tier | 備考 |
|-------|------|------|--------------|------|
| `speckit-analyze` | 260 | 既存 spec / docs を分析してギャップ・矛盾を検出する | **Tier 3** | 250 行超 / auto_load_prohibited |
| `speckit-checklist` | 372 | 機能の実装前チェックリストを生成する | **Tier 3** | 250 行超 / auto_load_prohibited |
| `speckit-clarify` | 254 | 要求の曖昧さを解消するための質問リストを生成する | **Tier 3** | 250 行超 / auto_load_prohibited |
| `speckit-constitution` | 157 | プロジェクト憲法（.specify/memory/constitution.md）を生成・更新する | Tier 2 | 必要時のみ読む |
| `speckit-implement` | 210 | 実装タスクを実行する | Tier 2 | **direct execution prohibited** — impl-review-loop 経由必須（下記参照） |
| `speckit-plan` | 152 | 機能の開発計画（plan.md）を生成する | Tier 2 | 必要時のみ読む |
| `speckit-specify` | 330 | 機能仕様（spec.md）を生成する | **Tier 3** | 250 行超 / auto_load_prohibited |
| `speckit-tasks` | 202 | spec から実装タスク（tasks.md）を生成する | Tier 2 | tasks.md は staging artifact / materialize 後 archived に降格 |
| `speckit-taskstoissues` | 106 | tasks.md から GitHub Issues を起票する | Tier 2 | `issue-author` / `create-issue` 経由で実行 |

### Tier 定義（speckit スキルにおける適用）

| Tier | 意味 | 読込タイミング |
|------|------|----------------|
| Tier 0 | ssot-registry / ADR summary 等 — 常時読む | 常時 |
| Tier 1 | 現在の feature spec compact — セッション開始時 | 必要なセッションのみ |
| Tier 2 | 作業手順・full design — 必要時のみ | 明示的に require したとき |
| **Tier 3** | archived / large artifact — auto_load_prohibited | 明示指示があるときのみ |

speckit-analyze / speckit-checklist / speckit-clarify / speckit-specify の 4 本は 250 行超のため **Tier 3** に分類する。
`CLAUDE.md` / `.claude/rules/` に常時読込（always / 常時 / autoload / auto-load）指示を追加することを禁止する。

### speckit-implement: direct execution prohibited（direct 実行を禁止する境界）

```yaml
speckit_implement_policy:
  direct_execution_on_main: prohibited
  reason: >
    ADR 0002 の implementation_execution_policy より。
    /speckit.implement や tasks.md からの直接実装は、
    既存の issue-contract-review / impl-review-loop / test-runner / pr-review-judge
    による ledger / review 経路を迂回するため禁止する。
  allowed_path:
    - github_issue
    - issue-contract-review
    - impl-review-loop
    - implementation-worker
    - test-runner
    - pr-review-judge
  supervised_spike_use: allowed_in_throwaway_worktree_only
```

**speckit-implement は impl-review-loop 経由必須 / direct execution prohibited**

### artifact 分類

`.claude/skills/speckit-*` は `reviewed upstream snapshot` / `managed derived artifact` として扱う。
- specify-cli v0.8.13 upstream から throwaway spike (#298) で生成・検証後に手動マージ済み
- 直接 `specify init` による再生成は禁止（ADR 0002 Stop Condition 準拠）
- upstream 更新時は別 Issue を起票して管理する
- provenance: `.specify/provenance/spec-kit-main-introduction.yml`

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

## Self-Report 単独 APPROVE 禁止原則

PR review では implementer の self-report 単独では APPROVE を決定してはならない（schema 検証段階で G2 gate により reject される）。
review verdict は external evidence（CI artifact、evidence_refs 構造、oracle 検証、head SHA consistency 等）に基づく必要がある（#371 G2 gate reference）。

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
| 有効な改善 | `triage-required` を外し、implementation issue canonical contract に沿って適切な `phase/` / `agent/` routing label を維持または付与する。着手可否は `issue-contract-review` の `status: go` と dependency close 状態で判定し、`state/queued` は付与しない |
| 重複 | 既存 Issue にコメントして close（`duplicate` ラベル） |
| 不要 | `not planned` で close（理由をコメントに記録） |
| 判断保留 | `state/needs-human` を付与して人間判断を仰ぐ |

## Follow-up Materialization Policy（follow-up 実体化方針）

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
  schema_version: 1
  materialized_by: post-merge-cleanup | issue-refinement-loop | impl-review-loop
  follow_up_issues:
    - request_dedupe_key: "follow-up:<repo>:<source-url-or-pr>:<note-id>"
      status: created | reused_open | skipped_closed_duplicate | skipped_closed_not_planned | skipped_closed_completed
      issue:
        number: 123        # status=created/reused_open の場合
        url: "https://github.com/..."
      # status=skipped_* の場合は issue: null
      reason: null         # skipped 時は理由を記載
  note_only_observations:
    - dedupe_key: "follow-up:<repo>:<source-url-or-pr>:<note-id>"
      source_url: "<観察元の URL>"
      source_note_id: "<note_id>"
      summary: "<観察内容の要約>"
```

> 必須: `follow_up_issues` / `note_only_observations` は空の場合も `[]` で出力すること（省略禁止）。`schema_version: 1` は常に付与すること。

各 skill（`impl-review-loop` Step 5 / `post-merge-cleanup` / `issue-refinement-loop`）の終了コメントは本スキーマを参照して `follow_up_issues` と `note_only_observations` を報告する。

**責務境界**:

- `pr-review-judge`: non-blocker observations を `FOLLOW_UP_ISSUE_REQUEST_V1` として `LOOP_VERDICT.follow_up_issue_requests` に出力する。**Issue 起票は行わない**。`follow_up_issues` フィールドを `LOOP_VERDICT` に出力してはならない（**negative rule**）。
- `post-merge-cleanup-worker`: `FOLLOW_UP_ISSUE_REQUEST_V1[]` を列挙して main thread に返す。**`gh issue create` / `issue-author` / `create-issue` を直接呼び出してはならない**。Issue の実際の作成は必ず main thread が担う。
- `post-merge-cleanup`（main thread cleanup phase）: `post-merge-cleanup-worker` から受け取ったリクエストを dedupe 後に `issue-author` / `create-issue` 経由で materialize する **terminal materializer**（terminal materialization coordinator）。follow-up の raw context を保持・判断する context owner ではなく、PR / impl-review-loop 由来の蓄積済み `FOLLOW_UP_ISSUE_REQUEST_V1[]` を終端で materialize・report する。
- `issue-refinement-loop`: scope split / out-of-scope discovery / child materialization の出口を持つ **thin orchestrator**。review-issue 由来の観察を routing するだけで、follow-up の raw context を保持・再解釈しない。終了コメントには materialization 結果（`FOLLOW_UP_MATERIALIZATION_RESULT_V1`）のみを出す。
- main thread（impl-review-loop Step 5 / post-merge-cleanup）: リクエストを受け取り、dedupe_key で dedupe チェック後に `issue-author` / `create-issue` 経由で起票する。

## CHILD_MATERIALIZATION_PLAN_V2

delivery-rollup parent の child materialization 制御スキームで使う plan スキーマ。
`.claude/skills/create-issue/scripts/plan_child_materialization.py` が生成し、`create-issue` / `edit-issue` / `issue-refinement-loop` / `impl-review-loop` / `open-pr` / `post-merge-cleanup` の各 skill が消費する。

> V1 からの主な変更点: closed child を `existing_closed` に正しく分類、section scoping により `## Child Issues` 外の child ID を無視、`parent_mode` 欠落時に `unknown` を返しデフォルト assumption を廃止、schema に `closure_mode` / `repo` / `source` / `generated_at` / `body_sha256` / `issue_lookup.complete` を追加、`parent_body_updates` を line-oriented patch 形式に変更、dry-run 時の issue ref を `existing_unverified` に分類。

### スキーマ定義

```yaml
CHILD_MATERIALIZATION_PLAN_V2:
  schema_version: 2
  repo: "squne121/loop-protocol"
  generated_at: "2026-05-24T00:00:00Z"
  source:
    kind: parent_issue_body
    issue_number: 254
    body_sha256: "<sha256 of body>"
  parent:
    issue_number: 254
    parent_mode: delivery-rollup    # 欠落時は 'unknown'
    closure_mode: child-complete    # 欠落時は 'unknown'
  issue_lookup:
    strategy: "referenced_issue_view_and_dedupe_search_all_states"
    complete: true                  # false の場合、consumer skill は mutation 禁止
    warnings: []
  children:
    - child_id: "C254-3"            # 例: C254-3
      title: "..."                  # child の期待タイトル（placeholder / issue ref を除去済み）
      status: missing | existing_open | existing_closed | existing_unverified | stale_body_only | ambiguous
      existing_issue:               # null or object
        number: 281
        state: OPEN | CLOSED
        state_reason: null | COMPLETED | NOT_PLANNED
        url: "https://github.com/..."
      action: create_issue | reuse_and_update_parent | no_op | human_escalation
      dedupe_key: "delivery-rollup:<parent_issue>:<child_id>"
      existing_issue_candidates: [] # dedupe search 結果
  parent_body_updates:              # stale_body_only child が存在する場合に生成
    - section: "Child Issues"
      line_number: 143              # 1-based; parent body 内の行番号
      old_line: "- C254-5 ...（未起票） #285"
      new_line: "- C254-5 ... #285"
      expected_match_count: 1       # 1 以外の場合は edit-issue が abort
  body_inventory:                   # AC2: candidate count vs parsed count の差分（parser gap 検出）
    candidate_count: 5             # _is_candidate_line() で検出した行数
    parsed_count: 4                # 実際にパースできた child 数
    parser_gap_report:             # parsed_count < candidate_count の場合に生成
      - line_number: 12
        raw_line: "- A issue without colon"
        gap_reason: unsupported_child_id_format   # GapReason literal
        suggested_repair: "- A: issue without colon"
        repair_confidence: high   # high | medium | low
        minimal_context: "..."    # gap 前後数行のコンテキスト
  github_subissues_actual:         # AC3: native GitHub Sub-issues API から取得した実際の sub-issues
    - number: 281
      title: "..."
      state: OPEN | CLOSED
      url: "https://github.com/..."
  required_issue_creations: []     # action=create_issue の child_id リスト
  required_issue_edits: []         # parent body 更新が必要な記述リスト
  warnings: []                     # 警告メッセージ（空でもキー必須）
```

### child.action の追加値（V2 拡張）

| action | 意味 |
|---|---|
| `register_subissue_or_human_escalation` | parent body に `#N` が存在するが native Sub-issues に未登録（AC4） |

### child.status の定義

| status | 意味 | action |
|---|---|---|
| `missing` | parent body に `(未起票)` と記載され、issue ref がない | `create_issue` |
| `existing_open` | parent body に有効な issue ref があり open issue が gh issue view で確認できる | `no_op` |
| `existing_closed` | parent body に issue ref があり closed issue が確認できる（child-complete では正常系） | `no_op` |
| `existing_unverified` | dry-run (`--body-file`) 時の issue ref（API 未確認） | `no_op` |
| `stale_body_only` | `(未起票)` と issue ref が共存（body drift 状態） | `reuse_and_update_parent` |
| `ambiguous` | issue ref は present だが gh issue view が失敗（存在不明） | `human_escalation` |

### issue_lookup.complete の消費ルール

- `complete: true` — 通常通り plan を処理する
- `complete: false` — consumer skill は GitHub Issue の mutation を行わない。human escalation として報告する

### 生成スクリプト

```bash
# GitHub から直接取得（read-only）
uv run --locked python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
  --repo squne121/loop-protocol \
  --issue 254

# ローカル fixture から取得（テスト・dry-run 用）
# NOTE: issue ref は 'existing_unverified' として分類される（API 未呼び出し）
uv run --locked python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
  --body-file fixtures/parent_254.md \
  --issue 254
```

スクリプトは read-only: GitHub Issue を変更しない。plan の mutation は `create_issue_txn.py`（create_issue action）と `edit-issue` skill（parent body update）が担う。

`edit-issue` は `parent_body_updates` の `expected_match_count != 1` を検出した場合、更新を abort する。

### skill 横断の消費フロー

```
plan_child_materialization.py
  → CHILD_MATERIALIZATION_PLAN_V2
    → create-issue (action=create_issue → create_issue_txn.py)
    → edit-issue (parent_body_updates → line-oriented patch / abort if expected_match_count != 1)
    → issue-refinement-loop (delivery-rollup gate)
    → impl-review-loop Step 5 (mandatory_follow_up)
    → open-pr (Parent Child Materialization section)
    → post-merge-cleanup Section 6 (残 child 検出)
```

## CHILD_MATERIALIZATION_RESULT_V2

`issue-author` SubAgent が `task: materialize_children` を実行した後に返す出力スキーマ。
`issue-refinement-loop` の Step 4.5 がこのスキーマを消費して `termination_reason` を決定する。

```yaml
CHILD_MATERIALIZATION_RESULT_V2:
  status: ok | partial_failure | failed | human_escalation
  created_issues:
    - child_id: "A"                     # plan.children[*].child_id に対応
      issue_number: 330
      issue_url: "https://github.com/..."
      action_taken: create_issue
  updated_parent: true | false          # parent body を edit-issue で更新した場合 true
  escalation_items:                     # human_escalation が必要な child のリスト
    - child_id: "B"
      reason: "repair_confidence: low — missing_title"
      raw_line: "- B: some description without #ref"
  errors:                               # 処理中にエラーが発生した child のリスト
    - child_id: "C"
      error: "create-issue failed: <error detail>"
```

### status の決定ルール

| status | 条件 |
|---|---|
| `ok` | `created_issues >= 1` かつ `errors` が空 |
| `partial_failure` | `created_issues >= 1` かつ `errors` が 1 件以上 |
| `failed` | `created_issues == 0` かつ `errors` が 1 件以上 |
| `human_escalation` | `escalation_items >= 1` かつ `errors` が空 |

### issue-refinement-loop Step 4.5 での消費ルール

| CHILD_MATERIALIZATION_RESULT_V2.status | termination_reason |
|---|---|
| `ok` | `approved`（Step 5 へ進む） |
| `partial_failure` | `human_escalation`（失敗した child ID をコメントに記録） |
| `failed` | `human_escalation` |
| `human_escalation` | `human_escalation`（escalation_items をコメントに記録） |

### child materialization executor（`materialize_child_issues.py` 実行境界）

`CHILD_MATERIALIZATION_PLAN_V2` を入力に取り、起票・parent patch・結果集約までを決定論的に実行して `CHILD_MATERIALIZATION_RESULT_V2` を返す executor。`.claude/skills/create-issue/scripts/materialize_child_issues.py` が正本実装で、`create-issue` skill のステップ 4b から呼ばれる。

```bash
uv run --locked python3 .claude/skills/create-issue/scripts/materialize_child_issues.py \
  --plan-file <CHILD_MATERIALIZATION_PLAN_V2 互換 JSON> [--gh <gh binary>]
```

設計境界:

| 項目 | 規約 |
|---|---|
| plan 検証 | closed schema。unknown key / duplicate `child_id` / `issue_lookup.complete: false` / 不正 `action` / 非整数 `depends_on` / 空 `allowed_paths` / AC↔VC set 不一致を fail-closed（非 JSON も拒否、YAML fallback なし） |
| body render | `ISSUE_TEMPLATE/<kind>.yml` の required label order を **strict loader**（template 不在 / malformed / 空ラベルは `PlanValidationError`、validator の後方互換 fallback を継承しない）で取得して生成（spec-driven）。Machine-Readable Contract は `yaml.safe_dump` で生成し、`allowed_paths` / `verification_commands` / `title` の control-char・backtick・code-fence break-out を schema 段で拒否。`validate_issue_body.py --kind --title` 通過が起票の前提 |
| 起票経路 | `create_issue_txn.py` のみ。`materialize_child_issues.py` は `gh issue create` を直接呼ばない。`--label-profile standard\|triage_only` も txn に転送 |
| dependency | `depends_on` を `create_issue_txn.py --dependency` に写像し、txn の `_readback_dependencies` で GitHub read-back まで確認。自由記述 dependency は schema 段で fail-closed |
| parent patch | `## Child Issues` section（exact heading regex、0 / 2 件以上は fail-closed）内で **必須** `parent.body_sha256` 一致 + exact `old_line` + `expected_match_count == 1` + section-scoped post-edit read-back を満たす場合のみ。`parent_body_updates` 非空時は `body_sha256` 必須。**`errors` または `escalation_items` が 1 件でもあれば parent patch を行わない**（mixed outcome は `partial_failure`） |
| overlap gate | `overlap.status: clear` は preflight provenance（`source` / `verdict ∈ {safe_new_issue, no_overlap}` / `checked_at` / `input_sha256`）必須で、untrusted plan 入力だけの bare `clear` は fail-closed。`not_run` / `deferred_to_issue` は各 create child が #948 を `depends_on` に持つことを要求し、欠落で `human_escalation`。`undeterminable` は無条件 `human_escalation` |
| result capture | `create_issue_txn` が `partial_failure` / `dedupe` で issue 番号を返した場合は `affected_issues` に記録し、silent loss を防ぐ（その run は `ok` にならず parent patch もしない） |
| exit code | `ok`=0 / `human_escalation`=3 / その他=1 |

`create_issue_txn.py` は AC3 として、internal validator 呼び出しに `--kind` / `--title` を転送する。`--issue-kind` が省略された場合は body の Machine-Readable Contract `issue_kind` を採用し、明示 `--issue-kind` と MRC kind が矛盾する場合は fail-closed する（両方空のときのみ後方互換で最小 static set を適用）。これにより caller が pre-validation を忘れても kind 固有の必須セクション / Stop Conditions / title prefix が fail-closed される。

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
    - branch_behind_main  # impl-review-loop Step 5 の BEHIND reroute 判定で使う routing-critical field
  LOOP_VERDICT:
    - verdict    # APPROVE | REQUEST_CHANGES
    - status     # ok | failed
  IMPLEMENT_RESULT_V1:
    - status     # ok | failed | blocked
```

詳細な domain judgment（blocking_issues のテキスト / diff_proposal の内容 / test failure の詳細 等）は、後続 SubAgent へ参照（GitHub comment URL / issue_url）として渡す。オーケストレーターが詳細を再解釈しない。

#### impl-review-loop V2 routing boundary（V2 ルーティング境界）

`impl-review-loop` Step 5 は `LOOP_VERDICT_V2.required_auto_actions` を canonical な routing source として扱う。`LOOP_VERDICT.recommendations` は V1 時代の stale wording であり、現行 consumer path の canonical field として復活させてはならない。

```yaml
impl_review_loop_v2_routing_boundary:
  TEST_VERDICT_MACHINE/v1.branch_behind_main:
    classification: routing_critical
  LOOP_VERDICT_V2.required_auto_actions[].kind:
    classification: routing_critical
  LOOP_VERDICT_V2.required_auto_actions[].executor:
    classification: routing_critical
  LOOP_VERDICT_V2.required_auto_actions[].skill:
    classification: routing_critical
  LOOP_VERDICT_V2.required_auto_actions[].expected_head_sha:
    classification: routing_critical
  LOOP_VERDICT_V2:
    negative_rules:
      - LOOP_VERDICT.recommendations must not be treated as a canonical routing field
      - unknown required_auto_actions.kind must fail closed
      - missing or mismatched expected_head_sha must fail closed before update_branch dispatch
```

`required_auto_actions[].kind` は action 種別の分岐点、`executor` / `skill` は data-plane 委譲先の決定、`expected_head_sha` は stale verdict を防ぐ race guard であり、いずれも routing-critical である。`branch_behind_main` は test-runner から Step 5 へ渡る補助信号で、BEHIND 状態の reroute 判断を `TEST_VERDICT_MACHINE/v1` 側から補強する field として扱う。

### 一時例外（temporary_exceptions）

以下は ORCHESTRATOR_IO_BOUNDARY_V1 の `forbidden_context` に対する一時的な例外として承認された項目:

```yaml
temporary_exceptions:
  - raw_anchor_comment_snapshot:
      owner: issue-refinement-loop Step 0/2
      reason: issue-227 first-stage scope. anchor comment classification は今回スコープでは main thread に残す
      allowed_until: follow-up issue (impl-review-loop boundary conformance check)
      constraints:
        - must not be forwarded raw to issue-author
        - must be normalized before Step 4
        - must not be used as generic reviewer_feedback_text
```

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

## Loop Sequencing & Preconditions（ループ順序と前提条件）

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

## Mandatory SubAgent Contract（必須 SubAgent 契約の正本）

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
> 各必須 phase で PR body 以外の証拠源を最低 1 つ要求する。PR body ledger は要約であり、正本の記録ではない。

## SubAgent Execution Ledger（SubAgent 実行 ledger の記録境界）

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

## OUTPUT_BUDGET_V1

全 SubAgent / Skill に適用する出力制約の定義。目的は「ルーティングに必要なスキーマフィールドを削除せず、人間向けサマリとエビデンスの再掲を削減すること」。

### 定義

```yaml
OUTPUT_BUDGET_V1:
  intent: "reduce completion/output bloat without removing routing-critical schema fields"
  max_human_summary_lines: 30
  max_human_summary_chars: 2400
  prohibit_full_body_reprint: true
  prohibit_full_diff_reprint: true
  machine_yaml:
    required_schema_fields: must_include_all
    optional_arrays_max_items: 5
    overflow: count_and_refs_only
  evidence:
    refs_only_by_default: true
    allowed_ref_forms: [url, "path:line-line", command_exit_code, artifact_id]
    short_quote_max_words: 25
  patch:
    minimal_delta_only: true
  escape_hatch:
    when_budget_blocks_blocking_finding: "emit NEEDS_EXPANSION with refs"
```

### 適用判定基準

本制約は `.claude/agents/*.md` と `.claude/skills/*/SKILL.md` の全ファイルに適用する。

| 対象 | 適用方法 |
|---|---|
| SubAgent の人間向けサマリ出力 | `max_human_summary_lines: 30` / `max_human_summary_chars: 2400` を遵守する |
| 機械可読 YAML 出力 | `required_schema_fields: must_include_all`（routing 必須フィールドは削らない）、オプション配列は 5 件まで（超過分は件数+参照のみ） |
| エビデンス・証跡 | 原則 `url` / `path:line-line` / `command_exit_code` / `artifact_id` の参照形式で示す。短い引用は 25 語以内を許容 |
| パッチ・diff | minimal delta のみ（前後全文の再掲禁止） |
| Issue / PR 本文の再掲 | 禁止（`prohibit_full_body_reprint: true` / `prohibit_full_diff_reprint: true`） |

### `escape_hatch` の条件

budget 制約の適用によりブロッキングな知見が伝達不能になる場合は、以下の形式で `NEEDS_EXPANSION` を emit して人間または orchestrator に判断を委ねる。

```
NEEDS_EXPANSION: <topic>
refs: [<url-or-path>]
```

`NEEDS_EXPANSION` は制約の例外ではなく、「参照を示した上で詳細展開を要求する」プロトコル。詳細を展開する責務は受け取り側が負う。

### 適用除外（non-goals）

- `machine_yaml` の `required_schema_fields` — routing に必要なフィールドは削らない
- `escape_hatch` 経由の `NEEDS_EXPANSION` — ブロッキング知見の隠蔽禁止
- VC や証跡で必須の出力 — OUTPUT_BUDGET_V1 適用により既存 VC が FAIL する場合は Stop Condition に該当

## Hook-based Ledger Optional Design（hook ベース ledger の任意設計）

Claude Code の hooks を使って SubAgent の開始・終了・結果を自動記録する設計概要。
**実装は別 Issue で行う**（本 Issue スコープ外）。

hook が記録する metadata の schema は [`docs/schemas/agent-session-manifest.md`](../schemas/agent-session-manifest.md) を参照。
`agent_session_manifest/v1` が各 phase（`main_loop` / `ledger_phase`）で残すべきフィールドの SSOT。

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

### session 記録の Kill Switch policy

session 記録ツール（EntireCLI 等）を導入・運用する際の Kill Switch 手順と `secrets_mode` 遷移時の session 記録制御については
`docs/dev/session-recording-policy.md`（`session_recording_policy/v1` SSOT）を参照する。

- SubAgent の transcript / local_file を public GitHub comment に添付することは禁止
- checkpoint remote は `private_verified` visibility のみ許可し、`unknown` の場合は fail-closed
- `secrets_mode != none` を検知したら Kill Switch を発動する

---

## CONTROLLED_SKILL_MUTATION_COMMAND_POLICY

（Issue #1166 — hooks: `publish_termination_report` 用 controlled mutation policy。公開系 mutation の境界。）

### 概要

`publish_termination_report.py` は GitHub に issue comment を投稿するリモートミューテーションを実行する。
このミューテーションを uncontrolled な直接呼び出しから切り離し、**単一の executor（`controlled_skill_mutation_exec.py`）経由のみ許可**するポリシー。

直接呼び出し（`python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py`）は worktree_scope_guard および local_main_branch_guard によって deny される。

### ポリシーレジストリ

```python
# scripts/agent-guards/controlled_skill_mutation_policy.py
CONTROLLED_SKILL_MUTATION_COMMAND_POLICY = {
    "termination_report.publish": {
        "command_id": "termination_report.publish",
        "executor_script": "scripts/agent-guards/controlled_skill_mutation_exec.py",
        "allowed_write_roots": ["artifacts/"],
        "github_mutation": {
            "comment_on_issue": True,
            "requires_repo": "squne121/loop-protocol",
            "requires_explicit_repo_flag": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ["artifacts/"],
        },
        "idempotency": {
            "marker_file_pattern": "artifacts/{issue_number}/termination_report_published.marker.json",
            "marker_field": "comment_id",
        },
        "env_sanitize": [
            "PUBLISH_ARTIFACT_DIR", "PYTHONPATH", "PYTHONHOME",
            "GH_EDITOR", "EDITOR", "VISUAL", "BROWSER",
        ],
    },
}
```

### 呼び出し形式

```bash
uv run --locked python3 scripts/agent-guards/controlled_skill_mutation_exec.py \
  --command-id termination_report.publish \
  --issue-number <int> \
  --input-file <path_to_TERMINATION_REPORT_INPUT_V1_json> \
  --repo squne121/loop-protocol
```

すべてのフラグが必須。`--flag=value` 形式・未知フラグ・重複フラグ・位置引数はすべて拒否（インジェクション防止）。

### Anti-split-brain（AC17、二重実装回避）

`is_controlled_skill_mutation_exec_command(cmd, project_root)` は `controlled_skill_mutation_policy.py` で一元定義され、
`worktree_scope_guard` と `local_main_branch_guard` の両方がこの関数を import して使用する。
各ガードが独立した allowlist を持つことを禁止（split-brain の原因）。

### 環境サニタイズ（AC11）

executor は以下の環境変数を除去した上で publisher を呼び出す:
`PUBLISH_ARTIFACT_DIR`, `PYTHONPATH`, `PYTHONHOME`, `GH_EDITOR`, `EDITOR`, `VISUAL`, `BROWSER`

### Idempotency（AC13、冪等性）

`artifacts/{issue_number}/termination_report_published.marker.json` が存在し `comment_id` フィールドを持つ場合、executor は再実行を拒否する（exit 0 + dry-run 出力）。

### --repo 固定（AC10）

`publish_termination_report.py` は `--repo <owner/repo>` を必須引数として受け取り、
`gh issue comment ... --repo <owner/repo>` に明示的に渡す。
`GITHUB_REPOSITORY` 環境変数や暗黙の gh デフォルトに依存しない。

### Module realpath 検証（AC16、realpath 一致確認）

executor は `publish_termination_report.py` の `__file__` realpath を検査し、
`.claude/skills/issue-refinement-loop/scripts/publish_termination_report.py`
に正規化されることを確認してから実行する。モジュールシャドウイングを防ぐ。

### settings.json wildcard と hook enforcement（wildcard と hook 強制の適用境界）

`.claude/settings.json` の `Bash(uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py *)` は
Claude Code permission syntax の制約上 `*` を使用しているが、settings は **最終 enforcement ではない**。

- settings は「このコマンドクラスを allow する UI 層」
- 実際の argv 制限・policy binding は `worktree_scope_guard.py` / `local_main_branch_guard.py` の hook 層で強制される
- wildcard を悪用した不正 argv（例: `--unknown-flag`）は hook の `_validate_executor_argv` によって deny される
- `test_worktree_scope_guard.py` / `test_local_main_branch_guard.py` の hook integrity tests が両軸（deny/allow）を確認

### OUTPUT_BUDGET_V1

routing-critical フィールド: `CONTROLLED_SKILL_MUTATION_COMMAND_POLICY` の全 key は必須。

## Issue metadata mutation executor lane（Issue #1284 メタデータ更新実行系統）

（Issue #1284 — Issue body/comment mutation と Contract Snapshot 投稿を、issue-specific
worktree ではなく Issue #1166 と同じ controlled executor lane から実行できるようにする拡張。）

`scripts/agent-guards/controlled_skill_mutation_policy.py` の
`CONTROLLED_SKILL_MUTATION_COMMAND_POLICY` に 3 command id を追加する:

- `issue_body.update` — Issue body の stale-write 防止付き更新
- `issue_comment.publish` — marker readback 付き Issue コメント投稿
- `contract_snapshot.publish` — publisher authority を
  `.claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py` に固定した
  Contract Snapshot 投稿

### Input file namespace（AC11、入力ファイルの名前空間）

3 command id の input file は `artifacts/{issue_number}/` 直下ではなく、
`artifacts/{issue_number}/issue-metadata/{command-id}/` subtree に統一する
（`termination_report.publish` の legacy namespace `artifacts/{issue_number}/` は
そのまま維持し、split-brain を避けるため command id ごとに subtree を分離する）。

### Per-command input schema（AC10、コマンド別入力スキーマ）

| command_id | input schema |
|---|---|
| `issue_body.update` | `ISSUE_BODY_UPDATE_INPUT_V1` |
| `issue_comment.publish` | `ISSUE_COMMENT_PUBLISH_INPUT_V1` |
| `contract_snapshot.publish` | `CONTRACT_SNAPSHOT_PUBLISH_INPUT_V1` |

command id と schema の不一致は mutation 前に deny される
（`_load_and_validate_input_json` が command id ごとの期待 schema を照合する）。

### issue_body.update の stale-write 防止（AC9）

input は `previous_body_sha256` / `previous_updated_at` / `new_body` / `new_body_sha256` を含む。
executor は mutation 直前に issue body/updatedAt を readback し、両方が input の
`previous_*` と一致しない限り mutation しない。mutation 後も body_sha256 を再取得し、
`new_body_sha256` と一致しない限り success にしない（片側のみ一致するズレも成功にしない）。

### issue_comment.publish の readback（AC4/AC14、投稿後の読み戻し検証）

input の `marker` 文字列が `comment_body` に埋め込まれていることを事前検証し、
投稿後は marker でコメントを検索する。marker が見つからない・複数見つかる場合は
success にしない。

### contract_snapshot.publish の publisher authority 固定（AC8）

executor は `ensure_contract_snapshot.py` を
`subprocess.run([sys.executable, <path>, "--issue-number", ..., "--repo", ...,
"--mode", "auto", "--post", "--artifact-dir", <policy 束縛 subtree>], shell=False)`
の argv-list 形式でのみ呼び出す。`--artifact-dir` は executor が
`artifacts/{issue_number}/issue-metadata/contract_snapshot.publish/` に固定して渡し、
呼び出し元が任意の書き込み先を指定することはできない。
`issue-contract-review` skill の production path（scripts/ 配下）に direct `--post`
呼び出しが存在しないことは
`.claude/skills/issue-contract-review/tests/test_contract_review_no_worktree_creation.py`
の negative test で固定する。

### env binding（AC15、環境変数との結び付け）

`termination_report.publish`（legacy）は `LOOP_ISSUE_NUMBER` 環境変数が必須のまま。
新 3 command id は `LOOP_ISSUE_NUMBER` が存在しない場合でも `--issue-number` と
repo binding のみで実行でき、存在する場合のみ `--issue-number` との一致を必須とする
（`_check_issue_env_binding` が command id ごとに分岐する）。

### hook allow（AC5、フックの許可）

`worktree_scope_guard.py` の `is_controlled_skill_mutation_exec_command` による
exact command class allow は executor script identity + argv 形状のみを検証し、
`--command-id` の値には依存しない。そのため新 3 command id は追加の allowlist entry
なしで同じ allow 経路に乗る。raw `gh issue edit` / `gh issue comment` は
`_classify_gh` により引き続き `mutating` に分類され、active issue + no-matching-worktree
状態では block される。

## edit-issue 向け transaction helper の契約定義（Issue #1287）

既存 Issue body/comment mutation の consumer contract は `edit_issue_txn.py` に集約する。
権限境界は helper input/result schema と controlled executor command id で固定する。

### ISSUE_EDIT_TXN_INPUT_V1

```yaml
type: object
additionalProperties: false
required:
  - schema
  - issue_number
  - repo
  - new_body_file
  - readiness_forwarding_payload
  - comment_mode
  - expected_previous_body_sha256
  - expected_previous_updated_at
  - title_update
properties:
  schema:
    const: ISSUE_EDIT_TXN_INPUT_V1
  issue_number:
    type: integer
  repo:
    type: string
  new_body_file:
    type: string
  readiness_forwarding_payload:
    type: object
    additionalProperties: false
    required: [readiness_result]
    properties:
      readiness_result:
        type: object
        additionalProperties: false
        required:
          - status
          - body_sha256
          - source_checks
          - errors
          - readiness_result_ref
        properties:
          status:
            enum: [go, needs_fix, human_judgment, input_or_runtime_error]
          body_sha256:
            type: string
          source_checks:
            type: array
            items:
              type: string
          errors:
            type: array
            items:
              type: object
          readiness_result_ref:
            type: string
          resolution_evidence:
            type: [string, "null"]
  comment_mode:
    type: object
    additionalProperties: false
    required: [mode]
    properties:
      mode:
        enum: [skip, publish]
      comment_body_file:
        type: [string, "null"]
      marker:
        type: [string, "null"]
  expected_previous_body_sha256:
    type: string
  expected_previous_updated_at:
    type: string
  title_update:
    type: object
    additionalProperties: false
    required: [required, proposed_title, reason]
    properties:
      required:
        type: boolean
      proposed_title:
        type: [string, "null"]
      reason:
        type: [string, "null"]
```

- `title_update.required == true` は v1 では no-mutation failure に倒す
- helper が生成する executor authority file は
  `artifacts/{issue_number}/issue-metadata/{command-id}/` 配下のみ

### ISSUE_EDIT_TXN_RESULT_V1

```yaml
type: object
additionalProperties: false
required:
  - schema
  - status
  - issue_number
  - repo
  - mutation_started
  - rollback_attempted
  - body_update
  - comment_publish
  - errors
properties:
  schema:
    const: ISSUE_EDIT_TXN_RESULT_V1
  status:
    enum: [ok, no_change, failed_no_mutation, failed_after_mutation, human_judgment]
  issue_number:
    type: [integer, "null"]
  repo:
    type: [string, "null"]
  mutation_started:
    type: boolean
  rollback_attempted:
    const: false
  body_update:
    type: object
    additionalProperties: false
    required:
      - attempted
      - status
      - previous_body_sha256
      - new_body_sha256
      - remote_current_body_sha256
      - artifact_ref
    properties:
      attempted:
        type: boolean
      status:
        enum: [not_run, ok, failed]
      previous_body_sha256:
        type: [string, "null"]
      new_body_sha256:
        type: [string, "null"]
      remote_current_body_sha256:
        type: [string, "null"]
      artifact_ref:
        type: [string, "null"]
  comment_publish:
    type: object
    additionalProperties: false
    required:
      - attempted
      - status
      - comment_id
      - comment_url
      - comment_body_sha256
      - artifact_ref
    properties:
      attempted:
        type: boolean
      status:
        enum: [not_run, ok, failed]
      comment_id:
        type: [string, "null"]
      comment_url:
        type: [string, "null"]
      comment_body_sha256:
        type: [string, "null"]
      artifact_ref:
        type: [string, "null"]
  errors:
    type: array
    items:
      type: object
      additionalProperties: false
      required: [code, message]
      properties:
        code:
          type: string
        message:
          type: string
```

- stdout は exactly one bounded JSON object とし、old/new issue body や raw child stdout/stderr を含めない
- body/comment mutation authority は `issue_body.update` / `issue_comment.publish` に限定する

## Parallel Agent Runtime Safety（並列エージェント実行安全性）

複数エージェント（SubAgent / worktree agent / 手動セッション）が同一 repo checkout を並行操作する際の安全境界を定義する（#1343 follow-up）。
`skill_runtime_exec.py` 自体の実装変更は本セクションの対象外（#1343 の Scope）。

### repo-wide snapshot は自プロセス副作用とみなさない

`git status` / `git diff` / ファイル一覧取得のような **repo 全体スナップショット系コマンド** の出力には、他エージェント（別 worktree・別セッション）が並行して書き込んだ変更が写り込むことがある。
これは自プロセスが引き起こした副作用ではなく、並行実行している別プロセスの書き込みが同一 working tree に反映された結果である。
executor / hook の postcondition 判定・rollback 判定は、snapshot 系コマンドの出力全体を「自分が引き起こした変更」として扱ってはならない。判定スコープは自プロセスが書き込んだ `allowed_write_roots` 配下に限定する。

### volatile roots（揮発性ルート）の扱い

以下のパスは複数エージェントの並行書き込みで内容が揮発的に変化しうる root として扱う。executor の postcondition 判定・fail-closed 判定では、これらの root 配下の変化のみを理由に単独で fail させない。

- `.claude/worktrees/**`（各 worktree agent の作業ツリー。同時に複数存在しうる）
- `.claude/artifacts/**`（複数 skill が並行して artifact を書き込む共有領域）
- `.guard_shadow_log.jsonl`（hook が並行 append するログファイル）
- `artifacts/session-manifest-runtime/**`（`.claude/hooks/session_manifest_debounce.mjs` /
  `.claude/hooks/generate_session_manifest_from_hook.mjs` が非同期に書き込む session-manifest
  telemetry 専用 subtree。`events/` / `locks/` / `tmp/` / `manifests/` の 4 サブディレクトリを持つ。
  #1409 follow-up。当該 subtree は hook 専有であり、`artifacts/{issue}/issue-metadata/{command-id}/`
  を含むそれ以外の `artifacts/**` は引き続き監査対象のまま）

executor / hook がこれらの root 配下の変化を検知した場合、`allowed_write_roots` に含まれるか、または既知の volatile root かを区別して判定し、他プロセス起因の変化と自プロセス逸脱を混同しない。

`scripts/agent-guards/skill_runtime_exec.py` では、この判定に用いるシンボル名は
`_RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS`（`_is_race_tolerant_unattributable_path` /
`_race_tolerant_unattributable_roots`）である（#1409 で `_VOLATILE_PEER_SESSION_ROOT_RELS` から改名）。
改名理由は、除外対象の変化が「他セッション由来（peer-session）」に限らず、「同一セッションの非同期
background hook worker」（session-manifest debounce/generate hook）由来のケースも含むためであり、
シンボル名はその共通性質（executor から見て provenance を区別できない = unattributable）を表す。

### 検出保証の二段階区分（Strict attribution mode / Race-tolerant stdlib mode）

並列実行安全性を主張する executor / hook が「self-write（自プロセスの逸脱書き込み）をどこまで検出できるか」は実装手段に依存する。
両者を区別せず一律に「volatile root への self-write も fail-close せよ」と要求すると、PR #1349（Closes #1343）が採用した
race-tolerant stdlib 実装の既知の制限（Known Limitation）と矛盾する。そのため保証水準を以下の二段階に分けて明記する。

#### Strict attribution mode（将来課題・実装は本セクションの対象外）

- 前提: `strace` 等 OS-level のプロセス単位トレースを用い、「どのプロセスがどのパスに書き込んだか」を厳密に区別できる実装。
- 要求: volatile root（`.claude/worktrees/**` 等）への書き込みであっても、それが自プロセス由来であり `allowed_write_roots` 外であれば fail-close する。
- 現状: 本 repo の executor / hook（`skill_runtime_exec.py` 等）は標準ライブラリのみで動作する方針（#1343 Out of Scope）であり、strict attribution mode は未実装。
  実装が必要になった場合は、本セクションを Required Design References に含めた別 Issue として起票する（本セクション自体は strict attribution の実装を要求しない）。
- `#1363`（Linux-only optional strict trace attribution mode、OPEN）が strict attribution mode の実装 Issue として存在する。
  `artifacts/session-manifest-runtime/**`（#1409）についての mode 別契約は以下のとおり:
  - race_tolerant_stdlib mode（現行）: `artifacts/session-manifest-runtime/**` への peer/background write は
    provenance を区別せず無条件で除外する（本節の Known Limitation どおり）。
  - strict_strace mode（`#1363` で実装予定）: OS-level トレースにより、traced child process 自身による
    `artifacts/session-manifest-runtime/**` への書き込みは通常の self-write と同様に fail-close の対象とする。
    一方、tracer の対象外である peer process / 非同期 hook worker（session-manifest debounce/generate hook 自身）
    による書き込みを、traced child の self-write に誤帰属してはならない（strict mode 導入後も hook 自身の
    正当な非同期書き込みを誤って fail-close させないことが要求される）。

#### Race-tolerant stdlib mode（現行実装。PR #1349 Option B）

- 前提: repo-wide snapshot（`git status` / `git diff` 等）の before/after 差分に基づく、標準ライブラリのみの実装。プロセス単位の attribution は行わない。
- 既知の制限（Known Limitation）: volatile peer-session root（`.claude/worktrees/**`、`.claude/artifacts/issue-refinement-loop/**` 等）への
  **自プロセスの** `allowed_write_roots` 外の書き込みは、他プロセス（peer session）由来の正当な並行書き込みと区別できないため検出されない
  （PR #1349 Safety Claim Matrix の Known Limitation を参照）。これは実装の見落としではなく、
  「他プロセスの並行書き込みを自プロセスの逸脱と誤認して fail させる」peer-session false positive（#1349 が修正した回帰）を避けるために
  明示的に受容したトレードオフである。
- negative test 要求: 並列実行安全性を主張する executor / hook の実装は、以下の negative test を最低 1 件含めることを要求する。
  - 「自プロセスが `allowed_write_roots` 外の **non-volatile な outside path**（`.claude/worktrees/**` 等の volatile root に含まれない
    任意の repo path。例: `.cache/outside.txt`）に書き込んだ場合は fail-closed する」ことを確認する test
  - volatile root の除外ロジックが「常に fail を握りつぶす」設計になっていないこと（他プロセス起因の変化は許容しつつ、
    non-volatile path への自プロセス起因の逸脱は検出する）を確認する test
  - **volatile root 配下への自プロセスの self-write 検出を要求する negative test は race-tolerant stdlib mode の対象外**
    （Known Limitation としてテストで明示することは許容されるが、それを fail-close させることは要求しない。
    strict attribution mode 導入までの既知の制限として扱う）。
- non-volatile outside path に対する negative test を伴わない「並列実行対応」の主張は、self-write bypass
  （自分の逸脱を volatile root 除外ロジックで隠蔽する抜け穴）を作りうるため、レビューで reject する。

### SubAgent 起動記録 writer の Linux 安全性の境界（Issue #1503）

`scripts/subagent-launch-ledger-writer.c` は、trusted local Codex hook process が
`artifacts/codex/subagent-launch-ledger.json` を更新する唯一の writer である。Node.js 側は
source を build して entry を渡すだけであり、ledger の read-modify-write、lock、temporary file、
replacement を直接実行してはならない。

- build artifact の配置（Issue #1502）: `ensureLedgerWriter()` は source を repo-local
  `tmp/subagent-launch-ledger-writer*` へ build しない。OS temp directory 配下の
  content-addressed cache（source の sha256 をキーにした再利用可能な build artifact。
  `os.tmpdir()/loop-protocol-subagent-ledger-writer-cache/<sha256>-ledger-writer`）へ build し、
  同一 source content の再ビルドを回避する。repo snapshot 外に置くことで、cold/warm な
  hook 呼び出しが `skill_runtime_exec.py` の anchor-bound preflight から self-write と誤認され
  ないようにする。repo-local `tmp/subagent-launch-ledger-writer*` を race-tolerant exception に
  追加することは行わない（directory-wide exclusion 禁止の原則と同様、Out of Scope）。

- helper は ledger parent directory fd を開き、Linux の `openat2`（利用できない kernel では
  `openat` + `O_NOFOLLOW`）と `renameat` を使用する。exclusive lock を取得した後に ledger を
  再読込し、valid `SUBAGENT_LAUNCH_LEDGER_V1` だけへ entry を追加する。
- ledger parent、ledger、lock、fixed temporary file に起動前から symlink、FIFO、socket、device、
  または期待外の entry がある場合、malformed JSON、schema invalid、lock timeout、build/write/rename
  failure を含めて fail-close とする。empty ledger への reset、partial JSON の公開、stale lock の
  自動削除は行わない。
- atomic replacement は reader が旧 ledger または完全な新 ledger のみを観測することを対象とする。
  power loss 後の durability、network filesystem、macOS/Windows の locking は保証しない。
- この境界は cooperating trusted local hook process を対象とする。hostile process が check/open/rename
  の間に path component を差し替える攻撃は security boundary として主張しない。strict_strace による
  process attribution は #1363 の handoff であり、本 writer の scope 外である。
- canonical evidence を最終利用する consumer は、少なくとも一つの launch と、選択された launch の
  `observed_dispatch` および `correlation` がすべて揃わない場合に fail-close する。

### 型付き ledger 遷移ポリシー（Issue #1502）

`skill_runtime_exec.py` が repo-wide snapshot/status diff を anchor-bound preflight の
postcondition 判定に使う際、SubAgent 起動 ledger まわりの書き込みは volatile root の
directory-wide 除外（前述の `_RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS`）を使わない。
`artifacts/`、`artifacts/codex/`、`tmp/` の directory-wide exclusion は禁止のままであり、
ledger 関連パスは以下の 4 分類のうち後半 2 分類として、exact path 単位・型付き遷移で扱う。

1. **directory roots**（前述の volatile root 分類。ledger とは別カテゴリ、変更なし）。
2. **stable exact peer file**: `artifacts/codex/subagent-launch-ledger.json`
   （`skill_runtime_exec.py` の `_LEDGER_STABLE_EXACT_REL`）。owner は
   `scripts/subagent-launch-ledger-writer.c`。canonical であり、consumer（他 skill /
   validator）が読む唯一の正本。許可される状態遷移は `absent -> regular` と
   `regular -> regular` のみ（`_is_allowed_stable_ledger_transition`。厳密な allow-tuple
   一致で判定し、`before_kind` を問わず `after_kind == "regular"` なら常に許可するような
   実装は禁止 -- symlink/directory/FIFO/socket/device からの `-> regular` 置換は明示的に
   拒否する）。delete（`regular -> absent`）、symlink、directory、FIFO、socket、device への
   置換は、before-kind に関わらずすべて fail-close する。`regular -> regular` はさらに型
   だけでなく内容も検証する（`_is_authorized_ledger_content_transition`）:
   before/after 双方が有効な `SUBAGENT_LAUNCH_LEDGER_V1` ドキュメントであること、
   `ledger_schema` / `generated_by` / `coverage_scope` が不変であること、`launches` /
   `root_thread_actions` が既存エントリを一切削除・変更しない append-only であることを
   要求する。malformed content への置換や既存エントリの削除・改変は、型としては
   `regular -> regular` で authorized でも content 検証で fail-close する。
3. **transient protocol entries**: `artifacts/codex/subagent-launch-ledger.json.lock` /
   `.tmp`（`_LEDGER_TRANSIENT_EXACT_RELS`）。owner は同じ native writer。writer 自身の
   単一呼び出しの間だけ存在する非 canonical な一時プロトコルエントリであり、consumer は
   これらを直接読まない。executor は子プロセス終了後、bounded quiescence window
   （既定 2 秒、50ms ポーリング。`_wait_for_ledger_transient_quiescence`）の間だけこれらの
   消滅を待つ。単発の空 poll だけでは quiescence とみなさず、短い confirm interval
   （既定 0.1 秒）を空けて再度空であることを確認できた場合のみ成功とする
   （TOCTOU: 単発 poll と呼び出し元の後続 snapshot 取得の間に late-arriving な
   lock/tmp が現れるケースを取りこぼさないため）。window 内に確認できずに消えれば無視し、
   window 経過後もなお残っていれば stale residue として fail-close する
   （`reason_code=ledger_transient_residue_timeout`）。stale lock/temp を executor が
   自動削除することはない。
4. **build/runtime executable artifact**: `check-codex-agents.mjs` の
   `ensureLedgerWriter()` が invocation ごとに `fs.mkdtempSync` で作る private work
   directory 配下のコンパイル済みバイナリ。共有・content-addressed な warm cache は
   廃止した（Issue #1502 REQUEST_CHANGES）: 予測可能なキャッシュパスに他プロセスが
   先回りで実行ファイルを配置できる TOCTOU / cache poisoning を防ぐため、毎回
   `os.tmpdir()` が repo 外の絶対パスであることを検証したうえで一意なディレクトリを
   作成し、読み取り済みの `sourceBytes` をそのディレクトリ内の private source file へ
   書き出してコンパイルし（`ledgerWriterSource` を pathname で再オープンしない）、
   子プロセス終了後に private directory ごと削除する。repo snapshot 外にあるため、
   この分類は `skill_runtime_exec.py` の snapshot/status diff に一切現れない
   （除外ロジックを必要としない）。

上記 3・4 に属さない `artifacts/codex/` 配下の他ファイル（sibling）は、この型付きポリシーの
対象外であり、通常の repo-wide snapshot/status diff がそのまま適用される。したがって
sibling の新規作成・更新・削除・rename は、既存の metadata/status model で観測可能な限り
引き続き fail-close する（Out of Scope: `artifacts/codex/` の directory-wide exclusion は
行わない）。symmetric-difference（create/delete）の delta 集合と、既存 path の
metadata-changed 集合は union で合成する。片方が非空でももう片方の計算をスキップしない
（mixed delta: ledger の新規作成と既存 sibling の更新が同一 invocation で同時に起きても、
sibling 側の更新差分を取りこぼさない）。

ledger の ancestor directory node（`artifacts`、`artifacts/codex`）の exemption も
postcondition だけでなく before-kind を snapshot して判定する。許可される ancestor 遷移は
`absent -> dir` と `dir -> dir` のみで、shallow から順に判定し、chain 内のどこかで
不許可な遷移が見つかった時点で、それより深い rel も safe set から除外する（parent が
symlink や plain file から real directory へ置換された場合、その parent 配下の deeper rel
単体の before/after だけを見ると `absent -> dir` に見えてしまうため、chain 全体の
安全性が伝播しないと見逃す）。

**stdlib snapshot mode の保証限界**: 本節の型付き遷移判定は「race-tolerant stdlib mode」
（前述）の一部であり、mtime/size ベースの metadata 比較を用いる。cooperating/trusted な
child process 自身による exact-ledger self-write（例えば正規 writer 以外のプロセスが
同じ内容で ledger を書き戻す byte-preserving update）を完全には attribution できない
（誰が書いたかを厳密に特定できない）。したがって、内容が変わらない `regular -> regular`
の byte-preserving update を検出しないことは既知の制限として受容する（本文書の
content-transition 検証はあくまで「内容が変化した場合」に適用され、無変更の
byte-preserving update 自体を新たに検出可能にするものではない）。strict な
process-level attribution（`strace` 等による OS-level trace）は本節の対象外であり、
`#1363`（Linux-only optional strict trace attribution mode、OPEN）が handoff 先である。

### Codex session manifest の canonical 出力先の外部化（Issue #1546）

Codex の Stop / SubagentStop hook（`scripts/session-recording/codex-hook-adapter.mjs`）が
生成する session manifest の canonical な出力先は、`CODEX_HOOK_MANIFEST_ROOT`（テスト分離
専用の override。本番では未設定）が未設定のとき、repository tree 内の `tmp/session-manifests/codex/**`
ではなく、Linux/WSL の canonical per-user state root（`XDG_STATE_HOME`、既定
`$HOME/.local/state`）配下へ移行した（`scripts/session-recording/resolve-codex-session-manifest-root.mjs`
の `resolveCodexSessionManifestRoot()`）。相対 path の `XDG_STATE_HOME` は無視され、既定値
（`$HOME/.local/state`）にフォールバックする。実際の write root は
`<XDG_STATE_HOME>/loop-protocol/session-manifests/v1/<repo_key>/codex/<event>/` であり、
`repo_key` は canonical repository URL と `realpath(repoRoot)` から導出した sha256 の先頭 32 hex
文字である（raw な local path や home directory を含まない）。

- **executor には session-manifest 固有の allowlist / typed transition policy を追加しない**
  （前述の「型付き ledger 遷移ポリシー」とは異なる設計判断）。write root が repository tree の
  外側にあるため、`skill_runtime_exec.py` の repo-wide snapshot/status diff（本節の対象）には
  そもそも現れない。したがって、独立 peer process による正規の external manifest write は
  `unauthorized_write_path` の false positive を起こさず、専用の除外ロジックも不要である。
- **repo-local write は引き続き fail-close する**。独立 peer が誤って（または悪意を持って）
  `tmp/session-manifests/**` へ書き込んだ場合は、既存の generic postcondition（この節の
  「repo-wide snapshot は自プロセス副作用とみなさない」以外の通常経路）がそのまま適用され、
  ordinary な unauthorized write として検出する。`_RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS`
  にも `_LEDGER_*` 型付き分類にも session-manifest 用のエントリは追加していない。
- **publish protocol**: writer（`scripts/session-recording/write-codex-session-manifest.mjs`）は
  create-once・no-overwrite プロトコルを実装する。一意な一時ファイル名で
  `openSync(..., 'wx', 0o600)` により exclusive create し、`fsync` 後、`linkSync()`（destination
  が既に存在すれば `EEXIST` で失敗し、`renameSync()` と異なり既存 final を上書きしない）で
  publish し、最後に一時ファイル名を unlink する。ディレクトリは `0700`、ファイルは `0600` を
  明示指定する。
- **evidence locator**: `manifest.evidence[].source_ref` と
  `manifest.secret_policy.runtime_boundary.evidence_ref` は、canonical な
  state-root-relative path（`resolveCodexSessionManifestEvidenceRef()`）で一致し、raw な
  home/repo absolute path や `..` を含まない。
- **脅威モデル**: `same_uid_malicious_peer: not_controlled`（native dirfd writer による
  `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)` / `renameat2(RENAME_NOREPLACE)` 相当の
  ハードニングは本 Issue では未採用。前述の SubAgent launch ledger writer とは異なり、
  cooperative same-UID 前提の pure Node 実装のままである）。
- **#1420 の supersede**: `tests/session_recording/codex/test_hook_adapter.py` の
  `test_manifest_written_to_default_root_when_env_unset`（旧 #1420 fix_delta AC12:
  「env 未設定時は repo-local `tmp/session-manifests/codex/stop/` に書く」という
  back-compat 契約）は、本 Issue（#1546）が supersede した。同テストは env 未設定時の
  production default が canonical external per-user state root であることを検証するよう
  更新され、レガシー repo-local root への新規 write が発生しないことも合わせて確認する。
  レガシー root に残る既存 artifact は自動削除しない（新規 write の停止のみ）。



`scripts/agent-ops/temp_residue_classifier.py` は root temporary residue（`tmp/`, `.claude/tmp/`,
`.tmp/`, `.temp/`, `.tmp-*/`）を read-only に走査し、`temp_residue_classification/v1`
（`schemas/temp_residue_classification_v1.schema.json`）を返す。

境界:

- classifier は `os.unlink` / `os.rmdir` / `shutil.rmtree` / mutation subprocess を一切呼ばない（read-only regression test で保証）。
- `recommendation: eligible_for_delete` は advisory であり、それ自体は deletion authorization ではない。実削除 executor は本 classifier の scope 外。
- consumer は `.claude/skills/post-merge-cleanup/scripts/classify-git-state.py`（`--format json` の `temp_residue_classification` field）と `.claude/skills/post-merge-cleanup/SKILL.md`。
- ownership marker（`temp_residue_owner/v1`、`scripts/agent-ops/temp_residue_marker.py`）は accidental isolation model のみを実装し、authorization model ではない（session id の真正性は保証しない）。
- denied alias（`.tmp/` `.temp/` `.tmp-*/`）配下は valid marker があっても常に `report_only`。approved roots（`tmp/` `.claude/tmp/`）配下の owned session directory のみ `eligible_for_delete` になり得る。詳細は `docs/dev/repository-folder-policy.md` の Root Class / Marker Effect Matrix を参照。
