---
adr_id: "0005"
title: "Agent skill surface sharing policy"
status: proposed
decision_date: "2026-06-11"
confirmed_date: null
issue_relations:
  implements:
    - "#779"
  downstream_consumers:
    - "#776"
    - "#780"
  non_precedent_references:
    - issue: "#381"
      reason: "session-recording-policy-specific SSOT and thin-pointer concern; not precedent for general custom-agent skill surface policy"
supersedes: []
superseded_by: null
---

# ADR 0005: Agent skill surface sharing policy

## Context

Issue #776 は、Codex custom agent が repo-local skill surface として `.claude/skills` に依存し続ける状態を解消し、Codex 側の repo-shared skill entrypoint を `.agents/skills/*/SKILL.md` に統一する implementation issue である。後続の #780 は `.codex/agents/*.toml` を thin runtime contract 化し、不要な workflow prose の重複を validator で防ぐ issue として切り出されている。

一方で、Claude Code と Codex はどちらも skill を `SKILL.md` ベースで扱うが、repo-local discovery surface は一致しない。Codex 公式 docs は repository skill discovery を `.agents/skills` とし、skill を directory + `SKILL.md` + optional `scripts/`, `references/`, `assets/`, `agents/openai.yaml` で定義している。Claude Code 公式 docs は project skill を `.claude/skills/<skill-name>/SKILL.md` に置き、description による自動起動、dynamic context injection、`allowed-tools` などの Claude-specific frontmatter を提供している。

この差分があるまま Claude 用 skill body と Codex 用 skill body を別々に増やすと、同じ workflow の自然言語本文を二重管理することになる。結果として、token 消費、context window 汚染、レビュー面積、security review surface、そして本文 drift のリスクが増える。

また、Issue #381 は `.agents/skills/session-recording-policy/SKILL.md` を Codex CLI から利用できるようにする session-recording-policy 固有 issue である。#381 は shared-body policy の一般原則を定める issue ではなく、session-recording-policy concern の thin pointer / SSOT concern として扱う必要がある。

本 ADR は、#776 / #780 の後続 implementation issue と PR が同じ判断基準を参照できるよう、Codex / Claude 間の skill surface sharing policy を decision record として固定する。

## Considered Options

**Option A**: Codex-only skill bodies を `.codex/skills/*/SKILL.md` に複製する
- メリット: Codex 専用の書き分けがしやすい
- デメリット: `.agents/skills` を Codex repo-shared surface に統一する #776 と衝突する。自然言語本文の重複管理が発生し、review / security / token cost が増える

**Option B**: `.agents/skills` を Codex repo-shared skill surface とし、shared body は symlink または thin wrapper で共有する
- メリット: #776 の surface policy と整合する。shared workflow を一箇所に寄せやすく、本文 drift を抑制できる。Codex / Claude の runtime 差分は wrapper や companion file に局所化できる
- デメリット: portability と platform-specific metadata の境界を明文化しないと、file symlink や frontmatter 差分で運用が崩れやすい

**Option C**: Claude / Codex の skill surface を完全分離し、内容差分を許容する
- メリット: 各 runtime に最適化しやすい
- デメリット: 同一 workflow の別文書化が常態化し、#780 の thin runtime contract 方針と逆行する。reviewer がどちらを正本として見るべきか不明確になる

**Option D**: skill 化をやめて `.codex/agents/*.toml` や agent 定義へ workflow 本文を直接埋め込む
- メリット: skill 参照を減らせる
- デメリット: subagent 定義の context が肥大化し、description / routing / dependency だけに絞る #780 の意図に反する。shared procedure の再利用性も低い

## Decision

**Option B を採用候補とする。**

Codex custom agent の repo-local shared skill surface は `.agents/skills/` を正本候補とする。`.codex/skills/` を第二の repo-shared skill surface として導入してはならない。

Claude Code 側の project skill surface は `.claude/skills/` のまま維持する。Claude 用 surface を消すことも、#779 時点で `.claude/skills/**` を全面移設することも本方針の目的ではない。

shared workflow を Codex / Claude 間で共有したい場合、`.codex/skills/*/SKILL.md` を separate Codex-only skill body として新設・増殖してはならない。将来 `.codex/skills/*/SKILL.md` bridge が必要になる場合でも、用途は shared skill body への **symlink** または **thin wrapper** に限定する。本文を独立複製した tracked Codex-only body は原則禁止とする。

Codex 公式 docs は `.agents/skills` を repository discovery surface とし、symlinked skill folders をサポートしている。一方で、今回確認できた一次情報は skill **folder** の symlink support であり、`SKILL.md` 単体の file symlink portability を同じ強さでは保証していない。このため、portability が未証明な場合の default は thin wrapper とする。

Claude Code 公式 docs は `.claude/skills/*/SKILL.md` を project skill surface とし、`allowed-tools`、`disable-model-invocation`、`user-invocable`、`context: fork`、`agent`、dynamic context injection (`!` command) などの Claude-specific capability を持つ。これらは shared body の portable subset には含めず、Claude-specific wrapper または companion file に残す。

この portable subset は「Claude と Codex の公式共通最小要件」ではなく、**この repo で cross-runtime shared body を扱うための policy** である。特に `name` と `description` を必須にするのは Codex repository skill compatibility を満たすための repo rule であり、Claude Code の最小要件をそのまま言い換えたものではない。

### Machine-readable policy

```yaml
agent_skill_surface_sharing:
  decision: proposed
  codex_repo_shared_surface: ".agents/skills/"
  claude_project_skill_surface: ".claude/skills/"
  codex_skills_directory_policy:
    default: "do_not_use_as_second_repo_shared_surface"
    allowed_only_as:
      - "symlink_to_shared_skill_body"
      - "thin_wrapper_to_shared_skill_body"
    tracked_independent_skill_bodies: prohibited
  separate_codex_only_skill_body:
    default: prohibited
    reasons:
      - "duplicate natural-language workflow bodies drift"
      - "larger review and security surface"
      - "higher context and token cost"
      - "conflicts with #776 canonical Codex surface"
  symlink_policy:
    directory_symlink:
      default: allowed_when_portability_validated
    file_symlink:
      default: not_default
      status: portability_not_confirmed_in_current_primary_sources
    thin_wrapper:
      default: preferred_when_portability_is_unproven
  codex_bridge_requirements:
    all_of:
      - "explicitly_marked_as_derived_and_non_canonical"
      - "contains_no_workflow_procedure_body_beyond_target_resolution_and_runtime_specific_caveats"
      - "canonical_target_is_.agents/skills/<skill-name>/SKILL.md"
      - "validator_proves_wrapper_is_thin"
  portable_shared_body_subset:
    required_frontmatter:
      - "name"
      - "description"
    prohibited_platform_specific_items:
      - "Claude-only frontmatter in shared body"
      - "Codex-only metadata in shared body"
      - "runtime-specific dynamic injection syntax when not portable"
  related_but_separate_concerns:
    "#381": "session-recording-policy specific concern; not precedent for general custom-agent skill surface policy"
  distribution_boundary:
    repo_scoped_workflow: "direct skill folder under .agents/skills/"
    installable_reusable_artifact: "package as a plugin"
    pseudo_distribution_surface_under_dot_codex_skills: prohibited
  downstream_consumers:
    - "#776"
    - "#780"
```

## Consequences

### Policy rules for future implementation issues

1. Codex repo-shared skill entrypoint を追加・修正する issue は `.agents/skills/<skill-name>/SKILL.md` を主語にする。
2. Claude project skill entrypoint は `.claude/skills/<skill-name>/SKILL.md` を主語にする。
3. `.codex/skills/` を repo-shared surface として前提化する issue / PR / validator は fail-closed にする。
4. `.codex/agents/*.toml` は workflow 本文の正本保存先ではなく、thin runtime contract と routing / dependency 記述に留める。
5. shared workflow を両 runtime で使う場合、まず shared body をどこに置くかを決め、その上で platform-specific wrapper が必要かを判断する。

### Distribution boundary

- repo-scoped workflow は `.agents/skills/` 配下の direct skill folder として管理する
- 他 repo や他開発者へ再利用可能な installable artifact として配布する場合は plugin packaging を選ぶ
- `.codex/skills/` を repo-local と distribution の中間のような pseudo-distribution surface として使ってはならない

### Shared body の最低条件

shared body として許容するのは、両 runtime で同じ意味に読める手順本文だけである。最低条件は次のとおり。

- この repo の portable subset policy として YAML frontmatter は `name` と `description` を持つ
- description は trigger と non-trigger boundary を先頭付近に書く
- workflow 本文は 1 つの job に絞る
- 長い reference、examples、補助資料は supporting files に逃がし、本文を過剰に肥大化させない
- runtime 固有 metadata は wrapper / companion file へ分離する

### Shared body に入れてはならないもの

shared body に次を直接埋め込んではならない。

- Claude-only frontmatter:
  `allowed-tools`, `disallowed-tools`, `disable-model-invocation`, `user-invocable`, `context`, `agent`, `hooks`, `paths`, `shell`, model/effort override を shared policy の正本として固定すること
- Claude-only dynamic context injection:
  `!` command や ````!` blocks` のような Claude-specific injection を portability 未確認のまま shared body の必須要件にすること
- Codex-only metadata:
  `agents/openai.yaml` に置く UI metadata, invocation policy, dependency declaration を shared body 本文へ混在させること

これらが必要な場合は、shared body を薄く保ち、platform wrapper または companion file に寄せる。

特に Claude の `allowed-tools` は「利用可能ツールの制限」ではなく、列挙ツールへの pre-approval を与え得る security boundary である。したがって `allowed-tools` を shared body から継承させてはならず、Claude-specific wrapper または companion metadata に留め、明示レビューを必須にする。

### symlink と thin wrapper の選択基準

**directory symlink を選んでよい条件**
- 参照先が skill folder 単位で安定している
- Codex / Claude 両方の discovery 挙動と、WSL, Windows checkout, GitHub Actions checkout, worktree 運用で portability を確認済みである
- repo reviewer が symlink target を容易に追跡できる

**thin wrapper を選ぶべき条件**
- portability が未証明である
- file-level symlink しか実現手段がなく、一次情報上の保証が弱い
- runtime ごとに frontmatter や companion metadata を分ける必要がある
- shared body への pointer と platform-specific 注意書きだけで要件を満たせる

### `.codex/skills/` bridge の最低条件

tracked な `.codex/skills/**/SKILL.md` 独立本文は許可しない。将来 `.codex/skills/**/SKILL.md` bridge を許可する場合は、少なくとも次をすべて満たす。

1. derived / non-canonical であることが明示されている
2. workflow procedure body を持たず、target resolution と runtime-specific caveat だけを持つ
3. canonical target が `.agents/skills/<skill-name>/SKILL.md` に固定されている
4. validator で thin wrapper だと証明されている

### 例外条件

次のいずれかに該当する場合のみ、shared body / wrapper / symlink 方針からの例外検討を認める。

- Codex と Claude で要求される frontmatter / invocation semantics が実質的に両立しない
- security review 上、runtime ごとに完全分離した本文が必要である
- portability 実証の結果、directory symlink も thin wrapper も deterministic に扱えない
- repo-local authoring ではなく配布単位として再設計する必要があり、plugin packaging を選ぶべきである

### 例外を許す場合の最低条件

例外を許すには、少なくとも次を満たす別 Issue または別 ADR が必要である。

- なぜ shared body / thin wrapper / symlink で解けないかの明示
- 追加される surface が token, context, review, security の各コストを増やすことの明示
- validator で何を fail-closed にするかの明示
- どの path が canonical で、どの path が derived artifact かの明示
- 後続 cleanup または migration plan の明示

### #381 との切り分け

#381 は `.agents/skills/session-recording-policy` を session-recording-policy concern として整備する issue であり、general custom-agent skill surface policy の precedent ではない。本 ADR は #381 の実装、配置変更、本文変更を扱わない。後続 issue / PR は、#381 を根拠に `.codex/skills/` を repo-shared surface として追加してはならない。frontmatter でも `#381` は downstream consumer や implementation source ではなく、non-precedent reference としてだけ扱う。

### #776 / #780 への引き継ぎ

- #776 は `.agents/skills/` を Codex repo-shared surface とする implementation の decision record として本 ADR を参照する
- #780 は `.codex/agents/*.toml` を thin runtime contract 化する際、workflow 本文を agent 定義へ再複製しない判断根拠として本 ADR を参照する
- validator / runtime contract は `runtime_dependency_status: codex_skill_required` など既存の canonical status と整合させ、`.claude/skills` を Codex runtime dependency として主張したら fail-closed にする

### Positive impact

- shared workflow 本文の drift を抑制できる
- token / context budget の無駄な重複消費を減らせる
- reviewer が canonical surface を一意に追いやすくなる
- Codex / Claude の runtime 差分を wrapper や companion metadata に限定できる

### Negative impact / trade-offs

- symlink portability を安易に前提化できない
- wrapper 設計を雑にすると pointer だけが増え、かえって探索コストが上がる
- runtime ごとの convenience field を shared body に直接書けないため、短期的には少し不便になる

## References

- Issue #779
- Issue #776
- Issue #780
- Issue #381
- Owner comment on #779: `https://github.com/squne121/loop-protocol/issues/779#issuecomment-4680357198`
- OpenAI Codex docs: Agent Skills `https://developers.openai.com/codex/skills`
- OpenAI Codex docs: Best practices `https://developers.openai.com/codex/learn/best-practices`
- OpenAI Codex docs: Subagents `https://developers.openai.com/codex/subagents`
- Anthropic Claude Code docs: Skills `https://code.claude.com/docs/en/skills`
- Anthropic Claude Code docs: Sub-agents `https://code.claude.com/docs/en/sub-agents`
