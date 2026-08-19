# LOOP_PROTOCOL 開発運用ワークフロー（SSOT）

LOOP_PROTOCOL における Issue 駆動開発の **単一の真実の情報源（SSOT）**。
個別 skill / agent / docs はこの文書を運用ルールの正本として参照する。

## 全体像（3 階層構造）

```
[SSOT]                  ← 開発運用ドキュメント（docs/dev/, docs/adr/, docs/product/）
   ↓
[確率論的プロンプト]    ← CLAUDE.md / Skill / Subagent 定義（AI に振る舞いを伝える）
   ↓
[決定論的ガードレール]  ← Claude Hooks / Git Hooks / GitHub Actions CI（物理強制）
```

| 階層 | 役割 | 実体 |
|---|---|---|
| SSOT | プロジェクトルールの正本（人間可読） | `docs/dev/workflow.md`（本ドキュメント）, `docs/dev/agent-skill-boundaries.md`, `docs/dev/github-ops.md`, `docs/adr/`, `docs/product/` |
| 確率論的プロンプト | AI 向け実行コンテキスト | ルート / per-directory `CLAUDE.md`, `.agents/skills/`（Codex custom agent の repo-local discovery surface）, `.claude/skills/`（Claude prompt surface / thin bridge が読む canonical body）, `.claude/agents/` |
| 決定論的ガードレール | AI 逸脱時の物理強制 | Claude Hooks（Issue #9）、Git Hooks（Issue #10）、`.github/workflows/ci.yml` |

SSOT を編集したら、対応する確率論的プロンプト層・決定論的ガードレール層を原則として同 PR で更新する。
ただし **policy-only PR** で Allowed Paths / ownership / 依存順の都合上その場で HOW 層や guardrail 層を同梱できない場合は、以下をすべて満たすときに限り例外を認める。

- follow-up Issue または parent-child 依存で、未反映の対応先が明示されている
- PR 本文または Issue comment に、未反映リスク・暫定運用・依存順が記録されている
- 「どの層がまだ未更新か」が監査可能で、merge 後に放置されない routing がある

確率論的プロンプト層は **設計判断の正本ではなく作業手順を伝える層** として扱い、SSOT 本文を長文で重複保持しない。
AI 向け手順は **不要な背景説明を抱え込まない** ように保ち、必要な section / script / reference を段階的に読む progressive disclosure を優先する。

## Issue 駆動開発フロー

```
[1] Issue 起票
       ↓ create-issue (issue-author SubAgent)
[2] Issue refinement (任意)
       ↓ issue-refinement-loop オーケストレーター
[3] 着手前 preflight
       ↓ issue-contract-review
[4] 実装 → 検証 → PR レビュー
       ↓ impl-review-loop オーケストレーター
[5] 人間レビュー → マージ
[6] post-merge cleanup
       ↓ post-merge-cleanup (post-merge-cleanup-worker SubAgent)
```

各フェーズで使う Skill / SubAgent の詳細は `docs/dev/agent-skill-boundaries.md` を参照。

### Phase 別の入口

| Phase | 起動方法 | 主要 Skill / SubAgent |
|---|---|---|
| Issue 起票 | 「Issue 起票して」「create issue」 | `create-issue` (via `issue-author`) |
| Issue 改善ループ | 「Issue ◯◯ を磨いて」「refinement loop」 | `issue-refinement-loop` |
| 着手前 preflight | 「Issue ◯◯ 実装の前確認」「contract review」 | `issue-contract-review` |
| 実装ループ | 「Issue ◯◯ をループで実装して」「`/impl-review-loop <N>`」 | `impl-review-loop` |
| 個別実装（loop なし） | 「Issue ◯◯ を実装して」「implement issue」 | `implement-issue` (via `implementation-worker`) |
| PR レビュー | 「PR ◯◯ レビューして」「review PR」 | `pr-review-judge` (via `pr-reviewer`) |
| マージ後 cleanup | 「クリーンアップして」「post merge」 | `post-merge-cleanup` (via `post-merge-cleanup-worker`) |

## テスト戦略（3 層責務分離 — Defense in Depth）

| レイヤー | 実行手段 | 実行内容 | 目的 |
|---|---|---|---|
| 1. AI 自己修復 | Claude Hooks (`PostToolUse`) | 編集ファイルの lint / typecheck | AI への即時フィードバック。CI 消費前のローカル fail-fast |
| 2. 履歴の保護 | Git Hooks (`pre-commit` / `pre-push`) | 高速検証 (typecheck / lint / unit test) | 壊れたコードが Git 履歴に刻まれるのを物理防止。E2E など重いテストは含めない |
| 3. 最終品質保証 | GitHub Actions CI | typecheck + lint + unit + E2E + build | クリーン環境での再現可能な最終確定。PR マージをシステムブロック |

同じテストを複数レイヤーで実行するのは **Defense in Depth（多層防御）**。

### テストスタイル

- **TDD（テスト駆動開発）**: 実装前に Vitest テストを書く
- **BDD（振る舞い駆動開発 = Behavior-Driven Development）**: テスト名・記述は GIVEN/WHEN/THEN 命名規則
- 実装詳細でなく入出力の振る舞いをアサーションする

## 1 Issue = 1 PR ルール

- 1 つの Issue に対して必ず 1 つの PR を作る
- 実装中に別の問題を発見した場合は新規 Issue を起票し、現 Issue のスコープを保つ
- 複数 Issue を 1 PR にまとめることは原則禁止
- skill 内・サブエージェント内でこのルールを物理強制する

## Execution Planning Policy (canonical SSOT)（実行計画方針）

`ISSUE_EXECUTION_DECISION_V1` は Issue の semantic execution planning を表す静的契約である。正本はこの節と
`.claude/skills/issue-refinement-loop/schemas/issue_execution_decision_v1.schema.json` であり、
`scope-rollup-policy.md` はこの正本を実行手順へ投影する文書である。planner は一回だけ決定を生成し、downstream consumer は semantic relation を再分類しない。

### Namespace と停止の分離

| Namespace | 値 / 役割 | downstream の扱い |
|---|---|---|
| semantic planning | `selected` / `deferred` / `blocked` / `duplicate` | `deferred` と `blocked` は planner の明示 state。後段の CI/review 等の safety stop と混同しない |
| freshness / integrity | `fresh` / `stale` / `incomplete` / `invalid` | 再収集または再計画する |
| Git collision observation | `clean` / `conflict` / `not_evaluable` / `stale` / `tool_error` | base / left / right / merge-base SHA、時刻、Git version、invocation mode を束縛する |
| GitHub merge readiness | `mergeable_state` と `merge_state_status` | `UNKNOWN` / null を clean に正規化しない |
| quality / safety | contract review、CI、review、security、permission、publication safety | 独立 gate として維持する |

collision-derived downstream stop reason は、SHA 束縛済みの Git merge conflict、または対象 PR の
`mergeable_state=CONFLICTING` / `merge_state_status=DIRTY` 観測だけに限定する。`BLOCKED`、`DRAFT`、`BEHIND`、
`UNKNOWN`、鮮度・完全性・contract review・CI・review・security・permission・publication safety は別 namespace の停止または再実行条件として残す。この限定は、下流停止理由を衝突観測だけに閉じ込め、他の失敗を意味的な衝突へ誤分類しないための規則である。

### 正規化された関係グラフ、完全性、consumer 互換性

- canonical artifact は `identity`（target issue/body SHA、生成時刻、collection digest）、issue_number 順の `nodes`、`(source_issue_number, target_issue_number, relation_type)` 順の `relations`、`execution`、`downstream_policy`、`completeness` の閉じた集合である。relation_type は `depends_on` / `duplicate` / `absorb` / `supersedes` / `coordinates` だけを許可する。
- `relations` は source → target の有向関係である。node/relation の順序・一意性、endpoint、self-edge、矛盾 parallel edge、`depends_on` cycle、target/predecessor/state/completeness の cross-field invariant と collection digest canonical preimage は #1677 の normative semantic validator が fail-closed で検証する。#1675 の JSON Schema はその静的 shape を閉じる。
- `execution.state` は `selected` / `deferred` / `blocked` / `duplicate`、`execution.predecessors` は target に入る `depends_on` relation と整合し、`defer_reason` は deferred/blocked 時に必須である。incomplete source または unresolved reference がある decision は selected にしない。
- downstream_policy は `semantic_reclassification: forbidden`、`freshness_validation: required`、`stale_action: rerun_issue_refinement` を固定する。consumer は freshness と compatibility を検証して opaque decision を consume し、semantic relation を再分類しない。

### 移行手順と hard gate

legacy `graph.nodes/graph.edges` + `execution.target_state/predecessor_issue_numbers/reason_codes` は adapter 入力に限る。mapping は `edges.relation` → `relations[].relation_type`、`target_state` → `execution.state`（`planned` は `deferred`）、`predecessor_issue_numbers` → `execution.predecessors`、`reason_codes` → `execution.defer_reason` とする。
移行 phase は `dual_write` → `equivalence` → `dual_read` → `new_authoritative` → `legacy_removed` とする。historical prose の `dual-write` は enum ではなく canonical `dual_write` を指す。equivalence は canonicalized digest が一致しない限り fail-closed で `migration_required: yes` とし、consumer inventory は legacy identifier と V1 の双方を明記する。この移行順序は、旧形式と V1 の等価性を確認し、互換性を失う変更を確実に拒否するための規則である。
`open-pr` の integrity、repository binding、freshness、CI/review、permission、publication safety hard gate は移行完了まで維持する。
planner / `open-pr` / `implement-issue` / `impl-review-loop` の production implementation、runtime state/handoff schema、外部サービス設定はこの policy-only contract の対象外である。

良い PR スコープの判定基準（`create-issue` Scope 判定で使う）:

| 基準 | 判定方法 |
|---|---|
| 単一意図 | 変更ファイル群が 1 つの Outcome のためだけに必要 |
| アーキ層のまとまり | Allowed Paths が 1 つの層（`src/state` / `src/render` / `src/systems` / `src/data` 等）に閉じている。複数層をまたぐ場合は層境界の変更そのものが Outcome |
| ロールバック単位 | 1 PR を revert すれば Outcome が完全に元に戻る |
| AC の独立性 | 各 AC が他の AC に依存せず、相互に独立に検証可能 |

## Worktree 配置規約

- 配置先: `.claude/worktrees/issue-<番号>-<slug>/` または `.claude/worktrees/<task-name>/`（リポジトリ内）
- `.gitignore` で除外済み
- `git worktree add` CLI を直接利用（特定エージェント専用機能には依存しない）
- リポジトリ外配置は禁止（Claude Code の workspace trust prompt が再発し承認マシーン化）

## Cross-runtime skill discovery（クロスランタイム skill discovery）

- `.agents/skills` は Git mode `120000` の root skill-directory symlink であり、link text は
  `../.claude/skills` に固定する。Codex は前者、Claude Code は後者から同一 skill package tree を読む。
- thin wrapper を追加せず、`SKILL.md`、`references/`、`scripts/` を package 単位で共有する。
- topology を変更した PR は linked worktree の fresh Codex / Claude Code discovery を実行し、inventory、
  重複なし、required file readback を artifact として保存する。既存 process は restart または reload 後に
  再検証する。

### マージ後クリーンアップ

PR マージ後は `post-merge-cleanup` skill 経由で自動的に:

```bash
git worktree remove .claude/worktrees/<slug>
git branch -d worktree-<slug>
```

## Issue / PR 種別とテンプレート

### Issue テンプレート（`.github/ISSUE_TEMPLATE/`）

| テンプレ | 用途 | 自動付与ラベル |
|---|---|---|
| `implementation.yml` | 実装作業 | `enhancement`, `phase/implementation`, `triage-required`, `agent/implementer` |
| `research.yml` | 仕様調査・比較検討 | `phase/research`, `state/queued`, `agent/research` |
| `parent.yml` | parent tracker（複数 child を束ねる） | `tracking`, `state/in-progress` |
| `bug-report.yml` | エンドユーザーバグ報告 | `bug` |
| `feature-request.yml` | エンドユーザー機能要望 | `enhancement` |

`human-confirm.yml` は不採用（PR #16）。人間判断は元 Issue 内でブロッカー扱い + 本文修正の運用とする。

Note: `research.yml` の `state/queued` は Known residual。#275 が `.github/ISSUE_TEMPLATE/research.yml` の writer cleanup を所有するため、本表の research 行も #275 完了時に同期更新する。

### Implementation issue canonical contract（実装 Issue の正規契約）

implementation issue では、以下 3 つを別概念として扱う。

#### Template auto-labels（テンプレート自動ラベル）

```yaml
implementation_template_auto_labels:
  - enhancement
  - phase/implementation
  - triage-required
  - agent/implementer
```

- 正本は `.github/ISSUE_TEMPLATE/implementation.yml`
- 自動付与ラベルは classification / routing 用であり、そのまま AI 着手可否の state machine に使わない

#### Consumer ready contract（consumer 着手可能契約）

```yaml
implementation_consumer_ready_contract:
  title_prefix:
    - "実装:"
    - "implement:"
  dependency_source_of_truth:
    - GitHub native issue dependency
    - line-anchored "Depends on #N" fallback
  dependency_required_state: all_closed
  contract_review:
    required: "CONTRACT_REVIEW_RESULT_V1 status: go"
```

- `impl-review-loop` / `implement-issue` / `issue-contract-review` はこの contract を正本として着手可否を判定する
- **SSOT 原則（#2084）: GitHub Issue labels は presentation-only（人間の認知・分類・検索補助）であり、AI の readiness・quality・implementation permission・handoff・stop condition の authority にしてはならない。** routing 可否や停止条件を label 名そのもので表現するスキーマキー（label-based gate）は本 contract に含めない。readiness authority は本 contract に列挙された non-label evidence（title prefix、GitHub native dependency close 状態、`CONTRACT_REVIEW_RESULT_V1 status: go`、GitHub native issue open/closed state）のみで再定義される。
- `triage-required` / `phase/implementation` / `agent/implementer` 等の label mutation は、readiness decision 後にのみ実行される best-effort presentation sync（telemetry-only。`applied | noop | failed`）として扱う。mutation の失敗は readiness failure にしない。

#### Triage profile（triage プロファイル）

```yaml
implementation_triage_profile:
  unresolved_default:
    - triage-required
  triaged_valid:
    remove:
      - triage-required
    preserve_or_add:
      - phase/implementation
      - agent/implementer
  human_escalation:
    - state/needs-human
```

- triage 完了後も ready 判定の primary signal は dependency close 状態と contract review 結果である

#### Deprecated legacy labels（非推奨 legacy ラベル）

- `state/queued` は deprecated / legacy であり、template auto-labels にも consumer ready contract にも含めない
- `state/queued` 不在だけで BLOCKED 判定しない
- `state/blocked` 残存だけで BLOCKED 判定しない

### PR テンプレート

`implement-issue` が生成する PR 本文の必須セクション（`open-pr` の Template Guard で強制）:
- `## Summary`
- `## 受け入れ条件の達成状況`
- `## 検証コマンド結果`
- `## Allowed Paths 遵守`

## Issue contract を作業計画の正本として扱う条件

`impl-review-loop` が GitHub Issue contract を作業計画の正本として扱い、追加の実装計画承認を要求しないための着手条件を以下に定義する。

### Hard gate（強制ゲート）

着手権限は取得時点の Issue と正規の連結作業ツリー識別情報に置く。
スコープ集約、重複確認、契約スナップショット、本文 SHA、起動台帳、セッション記録、
公開文脈、制御 executor の成果物は前提条件ではない。欠落、古い状態、不正、混在、
形式不良、フック未実行はいずれも警告に留める。

Codex のコマンド強制はリポジトリ内フックを権限根拠とせず、標準の隔離環境と
承認機構による `managed configuration` を正本とする。隔離済みの事前ツール実行
ガードについて `enforcement 再導入` を行う場合は、別 Issue で公式実行環境の
到達性と閉鎖的失敗契約を再検証してから管理対象設定として導入する。
安全停止は root checkout、detached HEAD、dirty worktree、Issue/branch mismatch、
Allowed Paths 違反、実テスト・CI・PR review failure に基づく。

### Codex custom-agent dispatch guardrail（Codex custom-agent 委譲ガードレール）

- Codex CLI では `impl-review-loop` / `post-merge-cleanup` の root thread は control-plane のみを担当し、data-plane 操作は明示 spawn した custom agent に委譲する
- `.codex/agents/*.toml` と dispatch validator は設定整合性の検査に使える。
  `SUBAGENT_LAUNCH_LEDGER_V1` は advisory telemetry のみで、missing/invalid を
  routing stop にせず、PASS・承認・CI・review・merge readiness の証拠にしない。
- parallel-safe ledger V2 は別 Issue で再設計し、未実装を通常 workflow の停止理由にしない。

### Multi-Agent V2 の V1 rollback（V1 への復帰手順）

Multi-Agent V2 の repository-pinned declaration を V1 に戻す必要がある場合は、
`.codex/config.toml` の `[features.multi_agent_v2]` で `enabled = false` に戻し、
`[agents]` table に `max_depth = 1` を復元する。その後、fresh session で次を再実行し、
rollback 後の config を checker が意図どおり判定することを確認する。

```bash
uv run --locked python3 scripts/check_impl_review_loop_codex_dispatch.py \
  --assert-project-multi-agent-v1-config
```

V1 rollback 状態では `--assert-project-multi-agent-v1-config` が、strict boolean
`enabled = false` と strict integer `max_depth = 1` の両方を正として PASS する。
意図的な失敗ではなく、期待する V1 状態への一致を rollback の証拠として扱う。

### human_escalation 後の Issue 本文変更と contract review 再実行（advisory、#1860 で hard stop から降格）

`human_escalation` で停止した後、Issue 本文を変更すると `body_sha256` が変化し、prior contract-review result が stale advisory となる（`issue-contract-review` の snapshot idempotency 機構参照）。この staleness 検出自体は上記 Hard gate（`## Issue contract を作業計画の正本として扱う条件` の `### Hard gate`）と同様、blocking authority を持たない advisory diagnostic である（#1860 Owner Decision）。

- prior `CONTRACT_REVIEW_RESULT_V1.status: go` の staleness は warning として記録するに留め、それ単独で `impl-review-loop` / `implement-issue` への handoff を禁止しない
- `issue-contract-review` の再実行は推奨されるが、着手の前提条件（prerequisite）ではない
- live Issue 本文（Outcome / AC / Allowed Paths / VC / Stop Conditions）が正本であり、stale な contract snapshot の有無にかかわらず、live 本文に基づいて作業を進めてよい

### branch publish retry の safety stop（publish 再試行の安全停止）

branch publish が hook / approval 境界で止まった場合、agent は manual remote update に暗黙フォールバックせず、まず live readback を行って `PUBLISH_LANE_DECISION_V1` を評価する。

- 比較対象: `expected_remote_head` / `current_remote_head` / `local_head` / `verified_head` / `declared_publish_head` / `allowed_paths_gate_status` / `remote_readback_source` / `decision_inputs_complete`
- `status: allow_retry` の場合だけ bounded publish command を再試行する
- `branch_mismatch` / `stale_remote_head` / `local_head_mismatch` / `remote_fast_forward_by_same_scope` / `remote_head_scope_contamination` / `allowed_paths_gate_not_ok` / `publish_guard_context_missing` / `publish_guard_context_invalid` のいずれかなら `PUBLISH_SAFETY_STOP_REPORT_V1` を残して停止する
- strict lane を hook に束縛する場合は `LOOP_PUBLISH_EXPECTED_REMOTE_HEAD` / `LOOP_PUBLISH_CURRENT_REMOTE_HEAD` / `LOOP_PUBLISH_DECLARED_PUBLISH_HEAD` / `LOOP_PUBLISH_VERIFIED_HEAD` / `LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS` / `LOOP_PUBLISH_REMOTE_READBACK_SOURCE` をセットする

### Scope Collision Preflight（スコープ衝突の事前確認、#1860 で advisory 化。#1679: implement-issue 側の peer Issue 再判定は撤去済み）

Allowed Paths overlap 単独では hard stop ではない。以下の class 分類は Issue 起票時の `create-issue` 側 preflight（`check_issue_overlap.py`）が使う概念であり、判定結果自体は着手・実装・PR publication を止めない（#1860: OPEN Issue 全件収集・semantic overlap 判定・Allowed Paths の文字列重複は advisory diagnostic であり blocking authority を持たない）。`implement-issue` / `open-pr` の実行時に peer OPEN Issue を再列挙・readback して class を再判定する処理（#1679 で撤去）は存在しない。`implement-issue` は target Issue・worktree・実 diff・実 test・target PR・CI・独立 review・human stop のみを判断入力とする target-only executor である。

- `C0: no collision`
  - Allowed Paths が重複しない。通常どおり着手可。
- `C1: benign overlap`
  - 同一ファイル・同一ディレクトリを含んでも、Outcome / AC / schema / output contract / heading が独立しており、片方の変更がもう片方を不要化しない。
  - 少なくとも以下をすべて満たす場合にのみ `C1` と判定する。
    - 同一 section / 同一 heading / 同一 machine-readable key を編集しない
    - Outcome / AC / schema / output contract が重複しない
    - 片方の変更がもう片方を不要化しない
    - PR 本文または実装記録に `related_issue` / `overlapping_paths` / `edit_intent` / `non_conflict_reason` を残す
  - 例: fixture 追加のみ、test file への独立 test case 追加、docs の索引・参照追記で heading / policy paragraph / output contract が衝突しないもの。
  - `C1` は着手可。監査証跡として重複 Issue 番号と benign overlap の理由を残すことが望ましい。
- `C2: ordered overlap`
  - 同じ schema / checker output / 関数境界などを触りうるが、依存順を明示すれば安全に直列化できる。
  - 例: 同じ Python checker への別 rule 追加、同一 schema key set の段階拡張。
  - `C2a`: predecessor が closed / merged 済みで、依存順が本文または parent に明記されている。着手可。
  - `C2b`: predecessor が open のまま。`Depends on #N` または parent Work Ordering を記録できるが、predecessor close を待たずに着手してもよい（advisory）。
  - 依存順が未記録の場合も着手を止めない。warning として記録する。
- `C3: conflicting overlap`
  - Outcome / AC / schema / ownership が実質的に同じ、または同時実装すると片方が不要になる可能性がある。
  - 例: 同じ bug の別修正、同じ checker rule の別名追加、同じ SSOT policy の競合変更。
  - `C3` は duplicate / superseded / absorb / split の候補として Issue コメントに記録するが、着手は自動停止しない（human escalation は advisory であり hard stop ではない）。

着手を停止するのは以下の場合のみ（#1860）:

- Hard gate 未充足（作業場所、protected paths/secret、破壊的 Git 操作禁止、typecheck/lint/test/build、CI/required checks/branch protection、独立レビュー・最終マージ）
- current head と対象 peer commit の実際の 3-way Git conflict
- target PR について GitHub が `mergeable == CONFLICTING` または `merge_state_status == DIRTY` と判定した場合

workflow 不具合の修正方針では、自然言語 workaround を先に積むのではなく、以下の順で **決定論的修正** を優先する。

1. 既存 script / checker / hook / CI で表現可能か
2. config / template / schema 変更で表現可能か
3. Skill / SubAgent の手順追記で扱う場合は、その理由と限界を PR 本文または Issue comment に記録する

少なくとも 1 または 2 が成立するのに自然言語 workaround だけで閉じる運用は採用しない。

#### 決定論的 overlap preflight helper（`check_issue_overlap.py`）

起票前の collision class 判定は title keyword search だけに依存しない。`.claude/skills/create-issue/scripts/check_issue_overlap.py`（overlap preflight helper）が title / goal_ref / Allowed Paths / labels / parent issue refs / dependency refs を機械判定し、`ISSUE_OVERLAP_PREFLIGHT_RESULT_V1`（`decision` / `reason_code` / `policy_class` / `source_status` / `candidates[].matched_fields` / `comment_template`）を返す。`decision` は closed enum（`duplicate` / `overlap_requires_comment` / `safe_new_issue` / `ambiguous_requires_human`）。

helper の `decision` は本ファイルの Scope Collision Classification（C0/C1/C2a/C2b/C3）を **再定義せず mapping** する:

```yaml
policy_mapping:
  exact_duplicate: duplicate
  C0: safe_new_issue
  C1: overlap_requires_comment
  C2a: overlap_requires_comment
  C2b: ambiguous_requires_human
  C3: ambiguous_requires_human
```

GitHub full-text search の false positive は候補 Issue body の `## Allowed Paths` read-back で除外する。GitHub source が失敗・partial・saturation のときは `ambiguous_requires_human`（fail-closed）。delivery-rollup parent の child 起票では sibling child 同士の Allowed Paths overlap も検査する（fixture-only、hard gate は #946 の責務）。

本 helper は preflight advisory / evidence producer であり、`create_issue_txn.py` の mutation hard gate ではない（#387 scope_collision_check との正規化共有は follow-up）。

> Claude Code の plan permission mode（`--permission-mode plan` / `Shift+Tab` / `/plan`）は人間が選ぶセッション単位の UI 制御であり、本ルールの対象外・着手条件判定に影響しない。

## Human Decision が必要な条件

以下に該当する場合、AI に丸投げせず人間が判断する:

- `src/state` ↔ `src/render` の境界変更
- 新しい外部依存（パッケージ）追加
- `assets/` / `LICENSES/` への変更（AI 編集禁止領域）
- 複数 Issue にまたがる仕様変更
- `CLAUDE.md` の制約変更
- 本ドキュメント（SSOT）の変更

ループ内では「ユーザーがループ起動した時点で routine 操作は承認済み」。詳細は `docs/dev/agent-skill-boundaries.md` の「ループ内の人間承認原則」を参照。

## docs 更新が必要な条件

| 変更内容 | 更新が必要なドキュメント |
|---|---|
| 開発フロー自体の変更 | 本ドキュメント（`docs/dev/workflow.md`） |
| アーキテクチャ境界の変更 | `docs/adr/` に ADR を追加 |
| 新機能の仕様追加 | `docs/product/` の仕様書を更新 |
| ディレクトリ構造の変更 | `docs/dev/directory-structure.md` |
| AI 向け実行手順の変更 | `.agents/skills/` / `.claude/skills/` / `.claude/agents/` |
| SubAgent / Skill 責務境界の変更 | `docs/dev/agent-skill-boundaries.md` |
| GitHub 運用ルールの変更 | `docs/dev/github-ops.md` |
| 物理強制ルールの追加 | `.claude/settings.json` のフック定義 + 該当スクリプト |
| GitHub Milestone 操作 | `docs/dev/milestone-ops.md` |
| 運用単位（issue-refinement-loop / impl-review-loop 等）の状態機械・SubAgent 契約・escalation 方針の変更 | `docs/dev/workflows/*.md`（derived_design_note） |

## Availability Invariant（可用性不変条件）

security / isolation 強化を目的とする変更（例: 認証情報の非露出化、実行環境の分離強化、trust boundary の縮小）は、
分離や遮断を強めるだけの変更になりがちで、結果として正規の canonical workflow 自体が実行不能になる回帰を静的検証だけでは検出できないことがある
（Issue #2241: Claude-GPT isolated session の `HOME` 分離強化が意図せず trusted `uv` toolchain 解決を壊した事例）。

このため、security / isolation 強化変更を含む delivery（Issue / PR）は、**同一 delivery 内で最低 1 つの canonical positive workflow を、
fresh isolated session から実証しなければならない**（Availability Invariant）。

- 「fresh isolated session からの実証」とは、`HOME` override や manual cache delete 等の workaround を用いず、
  対象の isolated 実行環境（例: Claude-GPT launcher が起動するセッション）をそのまま起動し、
  当該 canonical workflow（例: `preflight.run.with_human_context` 相当の control-plane command）が
  bootstrap / authentication 理由では停止せず完走することを指す。
- 動作検証の適用判定・SKIP 規約・証跡保存・Stop Condition 連動は `docs/dev/runtime-verification-policy.md` の
  「Runtime Verification Applicability」を正本とする。
- fallback 経由の成功（例: `HOME` override での代替成功）を canonical positive workflow の実証として扱ってはならない。

### Not Controlled（PR #2247 人間レビュー時点での既知の未解決事項）

Issue #2241 / PR #2247 の実装範囲では、以下は意図的に「未解決」として明記する（過大な安全主張を避けるため）:

- **credentialless GitHub read の production preflight への未接続**: `scripts/agent-guards/github_credentialless_read.py`
  は Issue 本体・comments（pagination 追従込み）を無認証で読む transport（`CredentiallessGitHubReadTransport` /
  `read_public_issue` / `list_issue_comments`）を提供するが、`.claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py`
  の `_fetch_issue()` / `_fetch_issue_comments()` はこの transport をまだ呼び出していない（引き続き `gh issue view` /
  `gh api --paginate` 経由）。このファイルは Issue #2241 の Allowed Paths に含まれておらず、本 Issue のスコープでは配線できない。
  isolated Claude-GPT session からの `preflight.run.with_human_context` 相当 command は、host `GH_TOKEN` 等が launcher に
  よって遮断されている限り、引き続き `gh` 認証失敗で停止しうる。配線には Allowed Paths 拡張（follow-up Issue または
  Scope Delta）が必要。
- **`~/.local/bin` trust root の CWE-427 search-selection risk（Issue #2251 で解消済み）**: PR #2247 時点では
  `scripts/agent-guards/skill_runtime_exec.py` の `_safe_path_entries()` が account-home `~/.local/bin` を trust root
  候補に含め、`_validate_account_local_bin_trust`（ancestor owner uid / group-world-writable 検証・symlink 解決先検証）
  と `_validate_local_bin_executable_version`（`--version` 出力の緩い pattern 照合）で防御していた。Issue #2251 は
  `~/.local/bin` を trust root 候補そのものから除外し（`_validate_account_local_bin_trust` は削除）、CWE-427 の
  search-selection element（攻撃者が制御可能な search path 要素を trust root に含めてしまうリスク）自体を縮小した。
  account-home `.local/bin` にしか `uv` が存在せず hostedtoolcache/system 標準ディレクトリからも解決できない場合、
  resolver は silent fallback せず `uv_not_found` で deterministic に fail-closed する。
- **同一 UID による executable 本体置換（引き続き Not Controlled）**: `~/.local/bin` を trust root 候補から除外して
  も、`_safe_path_entries()` が残す hostedtoolcache（`/opt/hostedtoolcache/uv` 等）や `/usr/local/bin` 等の
  システム標準ディレクトリ自体は、CI ランナーやホスト設定次第では依然としてこの実行アカウントと同一 UID で
  書き込み可能な場合がある。「この実行アカウント自身として既に任意コード実行を得た攻撃者が、trust root 配下の
  `uv` 実体そのものを置き換える」ケースは、`_validate_trusted_executable_version` による `pyproject.toml`
  `[tool.uv].required-version` 完全一致の `--version` 照合を加えても引き続き制御できない（同一 UID である限り、
  攻撃者は正規に見える `--version` バナーを返す偽 binary を用意できるため）。完全な解決には launcher-owned・
  child 非書き込みな専用 toolchain ディレクトリ、または dedicated user / OS-level sandbox の新設が必要であり、
  これは Issue #2251 のスコープを超える（CWE-427 相当の構造的制約が残る）。

## SSOT Routing Table（SSOT ルーティング表）

SSOT 追加時の参照先を集約した索引。AI エージェントは実装着手前に対象トピックの SSOT を本表から確認する。
カタログの正本は `docs/dev/ssot-registry.md` を参照すること。

| トピック | 参照先 SSOT |
|---|---|
| Issue 駆動開発フロー・1 Issue 1 PR | `docs/dev/workflow.md`（本ドキュメント） |
| SubAgent / Skill 責務境界 | `docs/dev/agent-skill-boundaries.md` |
| `gh` CLI 利用規約・ラベル運用 | `docs/dev/github-ops.md` |
| GitHub Milestone 作成・割当・close・rollup | `docs/dev/milestone-ops.md` |
| アーキテクチャ分離原則・60Hz タイムステップ | `docs/adr/0001-architecture-baseline.md` |
| SDD ツール採否・正本境界・token 対策・playtest 補正 | `docs/adr/0002-sdd-tool-adoption.md` |
| 全体要件・非ゴール | `docs/product/requirements.md` |
| 現在のフェーズ・優先項目 | `docs/dev/current-focus.md` |
| SSOT カタログ全体 | `docs/dev/ssot-registry.md` |
| product spec / `docs/product/**` ライフサイクル（作成・更新・archive・compact spec・spec delta・tasks.md adapter） | `docs/dev/product-spec-lifecycle.md` |
| MVP scope（MVP に含める / 含めない境界・success/failure/pivot criteria。`status: draft` の間は discovery 用であり implementation normative source ではない） | `docs/product/mvp-scope.md` |
| プレイテストの実施手順・フィードバック分類・PII 保護方針・Spec Delta Gate | `docs/product/playtest-protocol.md` |
| プレイテスト結果の記録・YAML テンプレート・スキーマ定義 | `docs/product/playtest-log.md` |
| ゲームロジック仕様（状態遷移・入力・時間モデル・衝突・勝敗・保存境界） | `docs/product/game-logic.md` |
| movement + projectile 最小仕様（player 移動・aim・fire・projectile 定数・座標系・lifecycle・テスト AC） | `docs/product/features/movement-projectile.md` |
| issue-refinement-loop / impl-review-loop の詳細設計（SubAgent 契約・state machine・escalation 分類）。architecture review / contract migration 時のみロード | `docs/dev/workflows/*.md` |
| Vite ビルド成果物の取扱 / 配布候補評価（Local / GitHub Pages / itch.io）/ RC checklist / M1 公開リリース判断基準 | `docs/dev/release-distribution-policy.md` |

### 新規 SSOT 追加時の必須更新セット

新しい SSOT 文書（`docs/` 配下）を追加する場合は、**同一 PR で以下をすべて更新する**こと:

1. **本表（SSOT Routing Table）** にエントリ追加
2. **`docs/dev/ssot-registry.md`**（SSOT カタログ正本）にエントリ追加
3. **`.claude/skills/ssot-discovery/SKILL.md`** の説明・例を必要に応じて更新

> 注意: `ssot-catalog.md` は PR #302 で削除済み（`ssot-registry.md` に統合）。以前の手順にあった「ssot-catalog.md にエントリ追加」は不要。

上記を同一 PR で更新しない場合、AI エージェントが新 SSOT を見落として古い情報で誤実行するリスクが生じる。

## Delivery-rollup Parent / Parent-mode Handoff 手順

`parent_mode: delivery-rollup` / `closure_mode: child-complete` の親 Issue を持つ child PR がマージされたとき、残り child を確実に起票・管理するための標準フロー。

### フロー概要

```
child PR マージ
  ↓
post-merge-cleanup Section 6a:
  plan_child_materialization.py --repo ... --issue <parent>
  → CHILD_MATERIALIZATION_PLAN_V2
    → missing children → follow_up_issue_requests (optional_follow_up)
    → stale_body_only → edit-issue (delivery-rollup-parent-update mode)
    → human_escalation → human_review_required: true
  ↓
main thread: dedupe チェック → issue-author / create-issue で起票
```

### 使用するスクリプト

```bash
# read-only plan 生成（GitHub から取得）
uv run --locked python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
  --repo <owner>/<repo> \
  --issue <parent_issue_number>
```

スキーマ正本: `docs/dev/agent-skill-boundaries.md#CHILD_MATERIALIZATION_PLAN_V2`

### skill 別の責務

| skill / SubAgent | delivery-rollup 特有の責務 |
|---|---|
| `create-issue` | `CHILD_MATERIALIZATION_PLAN_V2` の `action=create_issue` を `create_issue_txn.py` 経由で materialize する |
| `edit-issue` | `parent_body_updates` を backup / guard / rollback 付きで適用する（`delivery-rollup-parent-update` mode） |
| `issue-refinement-loop` | delivery-rollup parent approve 前に child materialization gate を実行する（Step 4.5） |
| `impl-review-loop` Step 5 | APPROVE 前に delivery-rollup parent の残り child を `mandatory_follow_up` として処理する |
| `open-pr` | PR 本文に `## Parent Child Materialization` セクションを追加する |
| `post-merge-cleanup` | Section 6a で delivery-rollup parent の残り child を検出し `follow_up_issue_requests` に追加する |

## 関連ドキュメント

- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界、オーケストレーター設計原則、ループ内人間承認原則
- `docs/dev/github-ops.md` — `gh` CLI 利用規約、Parent Mode、コメント記録テンプレ
- `docs/dev/directory-structure.md` — リポジトリ構造
- `docs/dev/current-focus.md` — 現在のフェーズ・優先項目
- `docs/adr/` — アーキテクチャ決定記録
- `docs/product/` — プロダクト仕様
- ルート `CLAUDE.md` — プロジェクト憲法（自動ロード）
- per-directory `CLAUDE.md` — 各層の不変条件

## 関連 Skill / SubAgent インデックス

詳細は `docs/dev/agent-skill-boundaries.md` を参照。

- Issue 管理系: `create-issue`, `edit-issue`, `review-issue`, `issue-contract-review`, `issue-refinement-loop`, `issue-author` (SubAgent)
- 実装系: `implement-issue`, `implementation-worker` (SubAgent), `test-runner` (SubAgent)
- レビュー系: `pr-review-judge`, `pr-reviewer` (SubAgent)
- オーケストレーション系: `impl-review-loop`, `open-pr`, `post-merge-cleanup`, `post-merge-cleanup-worker` (SubAgent)
- 補助系: `ssot-discovery`, `gemini-cli-headless-delegation`, `nlm-skill`, `codebase-investigator` (SubAgent)

- repo-local authoring/discovery surface は `.agents/skills/` を discovery、`.claude/skills/` を canonical body として分ける
- Codex 公式の `symlinked skill folders` support は確認済みだが、この repo では symlink portability is unproven; thin bridge is the default
- installable artifact として配布したい場合は direct repo surface を増やさず plugin packaging を別 concern として扱う
