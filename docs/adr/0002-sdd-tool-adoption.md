# ADR 0002: SDD ツール採否 — Spec-Driven Development 運用方針

```yaml
status: accepted
decision_date: 2026-05-23
confirmed_date: 2026-05-23
issue: "#257"
confirmation_issue: "#303"
parent_issue: "#254"
```

## コンテキスト

LOOP_PROTOCOL は `docs/dev/`, `docs/adr/`, `docs/product/` を SSOT（単一の真実の情報源）とし、
`.claude/skills/` と `.claude/agents/` をプロンプト層、Claude Hooks / Git Hooks / GitHub Actions CI を
決定論的ガードレール層として運用している。

Spec-Driven Development（SDD）の外部ツールとして Spec Kit および OpenSpec の採否を判断し、
既存 SSOT・Skill・SubAgent 運用との整合を確立する必要が生じた。

参照した一次情報:
- GitHub Spec Kit: https://github.com/github/spec-kit
- Spec Kit token 消費問題（reported risk）: https://github.com/github/spec-kit/issues/1492
- OpenSpec: https://github.com/Fission-AI/OpenSpec
- OpenSpec hard-coded schema 問題（open）: https://github.com/Fission-AI/OpenSpec/issues/666

```yaml
external_evidence_audit:
  checked_at: "2026-05-23"
  spec_kit_issue_1492:
    status: closed
    role: token-risk-input
    interpretation: >
      reported risk として参照。active blocker ではないが、token 対策（compact_spec / scoped_loading）の
      設計根拠として記録する。
  openspec_issue_666:
    status: open
    role: primary-adoption-blocker
    interpretation: >
      custom schema / 既存 docs format を hard-coded format が阻害する問題。
      「既存 docs/ の形を保ちたい」という今回の要件と正面衝突するため、
      OpenSpec primary 採用を見送る主根拠として記録する。
```

## 決定

```yaml
decision: accepted
previous_decision: accepted-with-deferral
confirmation_issue: "#303"
sdd_tool: spec-kit-upstream-compatible
canonical_source: docs-ssot
generated_artifacts: derived-workbench
openspec: compare-or-spike-only
ears: allowed-notation-in-docs-product
task_tracking: github-issues
tasks_md_role: staging-artifact
```

Spec Kit を upstream-compatible な方針で採用する。
Issue #298 の throwaway worktree spike（`spec_kit_spike_acceptance` 6 項目が条件付き PASS 以上）完了により、
`accepted-with-deferral` から `accepted` に確定した（Issue #303 で実施）。

OpenSpec は primary SDD tool として採用しない。比較対象・軽量 spike・将来再評価対象に留める
（OpenSpec Issue #666: hard-coded spec format が既存 `docs/` 形式との整合を阻害する）。

EARS（Easy Approach to Requirements Syntax）は SDD ツールとしてではなく、
要求記述 notation として `docs/product/` で部分採用する。

## Decision Points

### 1. SDD ツール採否

Spec Kit upstream-compatible を採用した（confirmed by #303）。
Spec Kit の `/speckit.constitution`, `/speckit.specify`, `/speckit.plan`, `/speckit.tasks`,
`/speckit.taskstoissues`, `/speckit.implement` の思想と template 分解を参考にしつつ、
既存 `docs/` SSOT を正本に据えたまま Spec Kit 思想へ寄せる。

実ツール導入（CLI インストール・`.specify/` ディレクトリ作成等）は Issue #298 の throwaway worktree spike で検証し、Issue #303 で main ブランチへの導入を完了した。

### 2. 正本境界（canonical_source と generated_artifacts）

```yaml
canonical_source: docs-ssot
generated_artifacts: derived-workbench
```

`docs/` が normative（正本）。`.specify/` 等の生成物は derived workbench artifact である。

**conflict_rule**:
- If `docs/` SSOT and generated artifacts disagree, `docs/` SSOT wins.
- Generated artifacts must not update project behavior unless reflected in `docs/` through PR.
- Until a future ADR explicitly changes the boundary, `docs/` remains normative.

`docs/` SSOT と generated artifact が矛盾した場合は `docs/` SSOT が勝つ。
`.specify/memory/constitution.md` 等を正本にすることは、既存 workflow の根本ルールを上書きする
設計変更になるため、本 ADR のスコープでは禁止する。

### 2.1 Override Mechanism Boundary

Spec Kit v0.8.13 の override / preset / extension 系の境界は、`docs/` が normative、
`.specify/` が derived workbench である前提を補強するために次のように固定する。

```yaml
override_mechanism_boundary:
  target_spec_kit:
    specify_cli_version: "specify 0.8.13"
    source_tag_or_commit: "v0.8.13 (b2314680fce898e0a9151b37ad2535d810c93eef)"
    evidence_doc: "docs/dev/spec-kit-override-investigation.md"
    docs_reference_rule: >
      Spec Kit docs and CLI behavior must be reviewed against the same immutable tag or
      commit. Do not mix latest or main documentation with an installed CLI version.
    upgrade_rule: >
      Before applying this boundary to a newer Spec Kit version, rerun the template
      resolution matrix, command behavior matrix, and spec-kit#2278 regression check.

  boundary_version_guard: >
    Keep this section pinned to specify 0.8.13 / v0.8.13 / b2314680fce898e0a9151b37ad2535d810c93eef;
    rerun docs/dev/spec-kit-override-investigation.md §3-A / §3-B and spec-kit#2278 before
    changing the tag or commit.

  mechanisms:
    project_local_template_overrides:
      decision: adopted
      scope: ".specify/templates/overrides/"
      rationale: "Runtime template resolution; low upgrade risk; single-repo local adaptation."

    preset:
      decision: conditional
      conditions:
        - "Only for multi-repository reuse"
        - "Pin source to immutable tag or commit"
        - "Review preset source before install"
        - "Record generated command snapshot diff in a dedicated PR"
        - "No network install during implementation loop unless explicitly allowed"
      rationale: "Preset is acceptable only when the source is pinned and reviewed as a reusable boundary."

    extension:
      decision: default_non_adopted
      rationale: >
        Extensions are for new commands, hooks, quality gates, and external integrations;
        overbroad for docs canonical policy. Future adoption must re-check v0.8.13
        docs and source precedence differences for extension template resolution.

    extension_hooks:
      decision: default_non_adopted
      rationale: "Runtime hook execution is not a canonical docs boundary mechanism."

    installed_command_registration_and_snapshot_overrides:
      decision: default_non_adopted
      rationale: >
        Spec Kit commands are registered into agent integration directories at install
        time. Local mutation is reviewed snapshot drift, not runtime override, and must
        not be used as the default docs-canonical boundary mechanism.
    installed_command_snapshot_overrides:
      alias_of: installed_command_registration_and_snapshot_overrides

  constitution_memory:
    decision: keep_as_derived_pointer
    operational_note: >
      /speckit-constitution may rewrite .specify/memory/constitution.md. If invoked,
      restore or update the derived pointer through a reviewed PR.
    canonical_refs:
      - "docs/adr/0002-sdd-tool-adoption.md"
      - "docs/dev/product-spec-lifecycle.md"
      - "docs/product/**"
```

### 3. Issue contract と tasks.md の責務分担

```yaml
task_tracking: github-issues
tasks_md_role: staging-artifact
```

| 役割 | 内容 |
|---|---|
| Parent Issue | 1 spec package / feature package の束ね |
| Spec Doc Issue | 1 normative spec doc 作成・更新 = 1 Issue = 1 PR |
| Implementation Task Issue | 1 independently verifiable task = 1 Issue = 1 PR |
| tasks.md | GitHub Issue へ materialize する前の一時的な作業分解（staging artifact） |

`tasks.md` は **staging artifact**（一時成果物）であり、tracking 正本ではない。
GitHub Issue が tracking 正本である。`tasks.md` から直接実装させると 1 Issue = 1 PR と衝突するため、
`taskstoissues` 相当の変換層を経由して GitHub Issue 化してから実装する。

materialize 後の `tasks.md` は `archived derived artifact` に降格する。
以降の tracking 正本は Issue / PR。

**issue_materialization_policy**（dependency preservation 必須）:

```yaml
issue_materialization_policy:
  source_artifact: tasks.md
  target: github-issues
  materialization_unit: one-task-one-issue-one-pr
  parent_issue: required
  source_task_id_trace: required
  source_requirement_id_trace: required
  dependency_source: github-native-dependency-or-depends-on-fallback
  direct_implementation_from_tasks_md: prohibited
```

`tasks.md` から materialize された Issue は、task 間の dependency を GitHub native dependency
または `Depends on #N` fallback として保持しなければならない。
dependency が materialize できない task は、implementation Issue として着手可能にしてはならない。

### 4. `.claude/skills` namespace collision policy

既存 13 skill（`create-issue`, `edit-issue`, `impl-review-loop`, `implement-issue`,
`issue-contract-review`, `issue-refinement-loop`, `open-pr`, `post-merge-cleanup`,
`pr-review-judge`, `ssot-discovery`, `gemini-cli-headless-delegation`, `nlm-skill`,
`runtime-verification-policy` 等）は置換しない。

namespace 隔離方針:
- Spec Kit 関連 skill は `spec-` prefix で命名する（例: `spec-doc-writer`, `spec-delta-issue`）
- `.claude/skills/` / `.claude/commands/` への Spec Kit 自動生成 artifact の書き込みは禁止
- 新規 skill 追加時は `docs/dev/agent-skill-boundaries.md` に責務境界を記録してから追加する
- 将来の routing 設計（例: `issue-refinement-loop` → `spec-doc-writer` SubAgent）は後続 Issue で実装

**implementation_execution_policy**（impl-review-loop 必須経路）:

Spec Kit の `/speckit.implement` や `tasks.md` からの直接実装は、既存 ledger を迂回するため禁止する。

```yaml
implementation_execution_policy:
  direct_speckit_implement_on_main: prohibited
  direct_tasks_md_implementation: prohibited
  speckit_implement_role: spike-only-or-human-supervised
  required_path:
    - github_issue
    - issue-contract-review
    - impl-review-loop
    - test-runner
    - pr-review-judge
  ledger_required: subagent_execution_ledger/v1
```

Spec Kit spike の受け入れ条件（throwaway worktree spike Issue で検証すること）:

```yaml
spec_kit_spike_acceptance:
  required_round_trip:
    - docs/product or docs/adr SSOT
    - derived Spec Kit artifact (.specify/ 等)
    - tasks.md staging
    - taskstoissues-equivalent GitHub Issues
    - implementation PR
    - docs SSOT update（PR 経由）
  forbidden_writes:
    - .claude/skills/**
    - .claude/commands/**
  required_checks:
    - generated artifact が docs/SSOT を上書きしない
    - existing skill / command namespace と衝突しない
    - tasks.md は materialize 後に archived derived artifact に降格する
    - 生成した tasks.md と既存 GitHub Issues が乖離しない
```

### 5. Token / コンテキスト消費対策（token_policy）

Spec Kit Issue #1492 で確認された問題: 生成 artifact が大きく冗長で、繰り返し再生成・再読込され、
短時間で usage limit に達する。

```yaml
token_policy:
  compact_spec: required
  scoped_loading: required
  full_regeneration: prohibited
  serena_mcp: code retrieval only
```

**compact_spec**（必須）: spec artifact の最大構成を制限する。
- 必須項目: `intent`, `requirement_id`, `acceptance_criteria`, `non_goals`, `trace_links`,
  `open_questions`, `playtest_hypotheses`
- 背景説明・重複 narrative・長い一般論は禁止

**scoped_loading**（必須）: Tiered Loading 方針。
- Tier 0: `ssot-registry.md` / catalog / ADR summary のみ常時読む
- Tier 1: 現在の feature spec compact のみ読む
- Tier 2: full design / playtest log は必要時のみ読む
- Tier 3: archived `.specify/` artifacts は自動読込禁止

**full_regeneration: prohibited**（明示禁止）: spec 全体を毎回再生成しない。
`REQ-xxx` / `TASK-xxx` 単位で diff-first 更新する。
「再生成された tasks.md が既存 Issue とズレる」状態を禁止する。

**serena_mcp: code retrieval only**（限定用途）: Serena MCP は LSP / symbol-level retrieval /
semantic code editing に使用する。Markdown spec の肥大化対策ではなく、実装時の code retrieval 対策である。
spec memory には `ssot-discovery` と registry を使う。Serena を spec authority として扱わない。

token policy の機械的ガード例（C254-2 の `product-spec-lifecycle.md` で詳細化すること）:

```bash
# compact spec サイズ guard（250 行上限）
test "$(wc -l < docs/product/<spec>.md)" -le 250

# generated artifact を正本化していないことの確認
! rg -n "canonical_source:.*(specify|openspec)|generated_artifacts:.*normative" \
    docs/adr docs/product 2>/dev/null

# full regeneration 禁止・diff-first 更新の明文化確認
rg -n "full_regeneration: prohibited|diff-first" docs/adr/0002-sdd-tool-adoption.md
```

### 6. Playtest-driven 補正（playtest_policy）

```yaml
playtest_policy:
  spec_compliant_but_not_fun: spec_delta_issue
  feedback_path: playtest-log -> spec delta issue -> implementation issue -> PR -> next playtest
```

「仕様通りだが面白くない」は implementation bug ではなく spec delta として扱う。

**feedback_path（フィードバックループ）**:

```text
playtest-log entry
  → classify: bug | balance/tuning | design hypothesis invalidated | unclear/needs-more-data
  → spec delta issue（docs/product/** 更新）
  → implementation task issue
  → impl-review-loop
  → next playtest
```

**spec delta gate（C254-7 への必須引き継ぎ条件）**:
- `design hypothesis invalidated` に分類されたエントリは、**必ず** spec delta issue を起票してから
  implementation task issue に変換する。直接 implementation task として扱ってはならない。
- `bug` は implementation task issue で扱ってよい。
- `balance/tuning` は spec delta issue を推奨するが、軽微な場合は implementation task でも可（PR 本文に理由を記載）。
- `unclear/needs-more-data` は追加 playtest session まで defer する。

`playtest-log.md` エントリの最小構造:

```yaml
playtest_entry:
  hypothesis_id: HYP-001
  observed_behavior: "<何が起きたか>"
  fun_failure: "<退屈・混乱・テンポ悪化など>"
  affected_requirements:
    - REQ-012
  decision: accept_spec_delta | reject | defer
  proposed_spec_delta: "<仕様変更案>"
  linked_issue: "#..."
  validation_method: "<次回どう確認するか>"
```

`playtest-log.md` テンプレートの作成および `docs/product/playtest-protocol.md` は C254-7 の責務。

## 結果と影響

### 後続 Issue への影響

本 ADR の決定により、後続 child Issue（C254-2〜C254-8）の実装方針が一意に決定可能になる。

- C254-2: `docs/dev/product-spec-lifecycle.md` — SDD ライフサイクル定義（compact spec / tiered loading の詳細）
- C254-3: `docs/product/game-thesis.md` — ゲームコンセプト正本（EARS notation 部分採用）
- C254-4: `docs/product/game-design.md` — GDD v0.1
- C254-5: `docs/product/game-logic.md`
- C254-6: `docs/product/mvp-scope.md`
- C254-7: `docs/product/playtest-protocol.md` + `playtest-log.md` template
- C254-8: `docs/dev/release-distribution-policy.md`

### 将来の ADR 候補

- Spec Kit CLI 本格導入（throwaway worktree spike 後、`spec_kit_spike_acceptance` の全条件を満たした場合）
- OpenSpec 再評価（Issue #666 解決後、かつ既存 `docs/` schema を adapter なしで扱えることが確認できた後）
- GitHub native Sub-issues 正式採用（別 ADR / GitHub ops issue）
- Spec Kit 生成物の正本化（本 ADR の conflict_rule を変更する場合）

## Policy Delta（Issue #303 確定時 — accepted 更新）

Issue #303（Spec Kit CLI main ブランチ導入）の実施により、以下のポリシーが確定した。

```yaml
policy_delta:
  issue: "#303"
  confirmed_at: "2026-05-23"

  skill_namespace:
    decision: upstream_name_adopted
    rationale: >
      ADR 0002 Decision Point 4 で「spec- prefix で命名する」方針を示したが、
      Issue #257 の ADR マージ後の検討（upstream-compatible 優先）により、
      speckit-* upstream 名をそのまま採用することを明示的な後続決定として確定する。
      既存の spec- prefix 方針よりも upstream-compatible 維持が優先される。
    adopted_names:
      - speckit-analyze
      - speckit-checklist
      - speckit-clarify
      - speckit-constitution
      - speckit-implement
      - speckit-plan
      - speckit-specify
      - speckit-tasks
      - speckit-taskstoissues

  skill_artifact_classification:
    path: ".claude/skills/speckit-*/"
    classification: reviewed_upstream_snapshot
    status: managed_derived_artifact
    description: >
      .claude/skills/speckit-* は specify-cli v0.8.13 upstream から throwaway spike (#298) で
      生成・検証後に手動マージした reviewed upstream snapshot である。
      直接 specify init による再生成は禁止し、upstream 更新時は別 Issue で管理する。

  implementation_execution_policy:
    direct_speckit_implement_on_main: prohibited
    description: >
      ADR 0002 で定めた direct_speckit_implement_on_main: prohibited が維持される。
      speckit-implement スキルは impl-review-loop 経由の supervised use のみ許可。

  docs_ssot_boundary:
    docs_wins: true
    specify_role: derived_workbench
    description: >
      .specify/ は derived workbench artifact であり、docs/ SSOT とは独立している。
      docs/ SSOT と .specify/ 生成物が矛盾した場合は docs/ が勝つ（conflict_rule 維持）。
      .specify/memory/constitution.md を docs/ の上位に置くことは引き続き禁止。

  tier_policy:
    tier3_skills:
      - speckit-analyze      # 260 lines
      - speckit-checklist    # 372 lines
      - speckit-clarify      # 254 lines
      - speckit-specify      # 330 lines
    loading_policy: auto_load_prohibited
    description: >
      250 行超の speckit-* SKILL.md は Tier 3 扱いで常時読込禁止。
      CLAUDE.md / .claude/rules/ に常時読込指示を追加してはならない。
```

## スコープ外

- 各 product doc（game-thesis / game-design / game-logic / mvp-scope / playtest / lifecycle 等）の本文作成
- CLAUDE.md や `.claude/rules/project-constitution.md` の「読む順序」更新
- 既存 13 skill / 9 SubAgent の改修
- speckit-* スキルの spec-* namespace へのリネーム（upstream-compatible 優先のため非実施）
