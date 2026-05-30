# Product Spec Lifecycle

```yaml
status: accepted
issue: "#283"
parent_issue: "#254"
normative_references:
  - docs/adr/0002-sdd-tool-adoption.md
  - docs/dev/workflow.md
  - docs/dev/ssot-registry.md
```

## 文書の位置づけ（policy / reference）

この文書は **policy / reference 文書**である。AI が直接実行手順として読む操作マニュアルではない。

- 具体的な作業手順（spec 作成・レビュー・修正・Issue 化）は、既存のループ（`issue-refinement-loop` / `impl-review-loop` / `issue-contract-review`）に従う。
- この文書は `docs/product/**` ライフサイクルの規則・制約・状態遷移を定義する参照 SSOT として機能する。

## Authority / Responsibility Boundary

This document defines the lifecycle of product SSOT documents (`docs/product/**`).
It does **not** redefine repository-wide execution workflow.

| Authority | Document | Scope |
|---|---|---|
| Repo-wide execution workflow | `docs/dev/workflow.md` | Issue phases, 1 Issue = 1 PR, worktree rules, quality gates, SSOT update set, delivery-rollup |
| Product spec lifecycle | This document | `docs/product/**` taxonomy, status transitions, creation / update / archive rules, compact spec, diff-first updates, EARS usage, trace links, spec delta flow, registry entry templates |
| SDD tool policy / generated artifact boundary | `docs/adr/0002-sdd-tool-adoption.md` | Spec Kit upstream-compatible adoption, `docs/` canonical, `.specify/` derived workbench, `tasks.md` staging artifact |
| SSOT catalog discovery | `docs/dev/ssot-registry.md` | Catalog entries and directory mappings only |
| Skill / SubAgent responsibilities | `docs/dev/agent-skill-boundaries.md` | `speckit-*`, native skills, orchestration, implementation worker |

**Conflict resolution order** (highest wins):
1. `docs/dev/workflow.md` — workflow execution rules
2. `docs/adr/0002-sdd-tool-adoption.md` — SDD tool policy / generated artifact boundary
3. Relevant `docs/product/**` SSOT — product content
4. `docs/dev/ssot-registry.md` — catalog discovery

## Product SSOT Taxonomy

Product SSOT documents live in `docs/product/`. Two tiers exist:

### Top-level product SSOT

Normative documents defining the product as a whole:

| Path | Content |
|---|---|
| `docs/product/requirements.md` | Global requirements / non-goals (existing) |
| `docs/product/game-thesis.md` | Product thesis / design pillars (C254-3) |
| `docs/product/game-design.md` | GDD-level design (C254-4) |
| `docs/product/game-logic.md` | Implementation-facing game rules (C254-5) |
| `docs/product/mvp-scope.md` | Current MVP boundary (C254-6) |
| `docs/product/playtest-protocol.md` | Playtest execution procedure & Spec Delta Gate (C254-7) |
| `docs/product/playtest-log.md` | Playtest log template & entry schema SSOT (C254-7) |

### Feature-level spec

Optional per-feature detail documents:

| Path | Content |
|---|---|
| `docs/product/features/<feature>.md` | Feature-level detail (standard placement per `project-constitution.md`) |

Feature specs follow `project-constitution.md` §"feature spec の標準配置" (YAML frontmatter, feature ID, status, related issue, acceptance, non-goals, related tests).

## Lifecycle States

```text
draft  →  accepted  →  superseded  →  archived
  ↑             ↓
  └─ (revision) ┘
```

| State | Meaning |
|---|---|
| `draft` | Under authoring; not yet the normative reference for implementation |
| `accepted` | Normative. AI agents must read before implementing affected scope |
| `superseded` | Replaced by another document; link to successor required |
| `archived` | No longer active; traceability preserved, file retained |

### YAML frontmatter required fields

Each `docs/product/**` document must include a YAML frontmatter block:

```yaml
status: draft | accepted | superseded | archived
issue: "#<N>"          # Issue that created or last changed the spec
parent_issue: "#<N>"   # Parent tracker issue (omit for top-level if none)
superseded_by: "#<N> / docs/product/..."  # required when status: superseded
archived_reason: "<text>"   # required when status: archived
archived_date: "YYYY-MM-DD" # required when status: archived
```

### Archive rules

```yaml
archive_rule:
  delete_file: prohibited      # deletion breaks traceability
  required_when_archived:
    - superseded_by
    - archived_reason
    - archived_date
    - linked_issue
```

Deleting a file is prohibited. Status transitions to `archived` or `superseded` with back-links.

## Creation Rules

When creating a new `docs/product/**` document:

1. Add YAML frontmatter with `status: draft` and `issue` reference.
2. Apply **compact spec** format (see §Token Policy below).
3. Use **EARS notation** for AC fields (see §EARS Usage).
4. Include `non_goals` and `trace_links` sections.
5. Register in `docs/dev/ssot-registry.md` in the same PR (see §Registry / Discovery Rules).
6. Add to `docs/dev/workflow.md` SSOT Routing Table in the same PR.
7. Add conditional reference in `CLAUDE.md` or `project-constitution.md` (see §Loading Policy).
8. Open a corresponding GitHub Issue for every doc addition (1 Issue = 1 PR; `tasks.md` staging adapter — see §tasks.md Adapter).

## Update Rules

When updating an existing `docs/product/**` document:

- Apply **diff-first** updates: change only the affected `REQ-xxx` / `TASK-xxx` / section, not the full document.
- `full_regeneration: prohibited` — do not regenerate the entire spec. Regenerated output often diverges from existing GitHub Issues.
- Update `issue` frontmatter field to reference the PR's linked Issue.
- If the update invalidates prior AC, classify as a **spec delta** (see §Product Spec Delta Flow).

## Token Policy

Derived from `docs/adr/0002-sdd-tool-adoption.md` §"Token / コンテキスト消費対策（token_policy）":

```yaml
token_policy:
  compact_spec: required
  scoped_loading: required
  full_regeneration: prohibited
  serena_mcp: code retrieval only
```

### compact_spec (required)

Product spec files must stay within **250 lines**. Required fields:

- `intent`
- `requirement_id` (e.g. `REQ-001`)
- `acceptance_criteria` (EARS notation preferred)
- `non_goals`
- `trace_links`
- `open_questions`
- `playtest_hypotheses` (when applicable)

Background narrative, duplicated prose, and lengthy generalities are prohibited.

Machine guard:
```bash
test "$(wc -l < docs/product/<spec>.md)" -le 250
```

### scoped_loading (required)

Tiered loading — AI agents load by need:

| Tier | Load when | Content |
|---|---|---|
| Tier 0 | Always | `ssot-registry.md`, ADR summary, SSOT Routing Table |
| Tier 1 | Current feature scope | Compact spec for the feature being implemented |
| Tier 2 | Design / playtest review | Full design doc, playtest log |
| Tier 3 | Never auto-load | Archived `.specify/` artifacts |

`product-spec-lifecycle.md` itself is Tier 1: load only when creating, updating, or archiving `docs/product/**` content.

### full_regeneration: prohibited

Do not regenerate a full spec from scratch. Use requirement-level diffs:
- `REQ-xxx` level changes only
- Re-generated `tasks.md` must not diverge from existing GitHub Issues

Machine guard:
```bash
! rg -n "canonical_source:.*(specify|openspec)|generated_artifacts:.*normative" \
    docs/adr docs/product 2>/dev/null
```

## EARS Usage

EARS (Easy Approach to Requirements Syntax) is used as a lightweight notation for AC in `docs/product/**` documents. EARS is a notation, not a new SDD tool SSOT.

Common EARS patterns:

| Pattern | Template |
|---|---|
| Ubiquitous | `The <system> shall <action>.` |
| Event-driven | `When <trigger>, the <system> shall <action>.` |
| Unwanted behavior | `If <condition>, the <system> shall <action>.` |
| Option | `Where <feature>, the <system> shall <action>.` |

Example:
```text
REQ-001: When a playtest session ends, the system shall record a playtest-log entry with hypothesis_id, observed_behavior, fun_failure, affected_requirements, and decision fields.
```

EARS is applied only at the AC level. Do not introduce EARS tooling or make EARS the spec format authority.

## Product Spec Delta Flow

Derived from `docs/adr/0002-sdd-tool-adoption.md` §"Playtest-driven 補正（playtest_policy）":

```text
playtest-log entry
  → classify: bug | balance/tuning | design hypothesis invalidated | unclear/needs-more-data
  → spec delta issue (docs/product/** update, 1 Issue = 1 PR)
  → implementation task issue (scoped to code changes)
  → impl-review-loop
  → PR merge
  → next playtest session
```

Classification rules:

| Category | Handling |
|---|---|
| `bug` | Implementation task issue (no spec delta required) |
| `balance/tuning` | Spec delta issue recommended; implementation task acceptable for minor changes (reason in PR body) |
| `design hypothesis invalidated` | Spec delta issue **required** before implementation task |
| `unclear/needs-more-data` | Defer to next playtest session |

`design hypothesis invalidated` entries must not be converted directly to implementation tasks.

## tasks.md Adapter

`tasks.md` is a **staging artifact** — a temporary decomposition before GitHub Issue materialization.
It is not a tracking SSOT. GitHub Issues are the tracking SSOT.

```yaml
product_task_materialization:
  source_artifact: tasks.md
  target: github-issues
  required_trace:
    - product_spec_id
    - requirement_id
    - source_task_id
    - parent_issue
  dependency_preservation: required
  direct_implementation_from_tasks_md: prohibited
```

**Materialization flow:**
```text
tasks.md (staging artifact)
  → taskstoissues-equivalent conversion
  → GitHub Issue (tracking SSOT)
  → issue-contract-review
  → impl-review-loop
  → PR
```

**Prohibited paths:**
- Direct implementation from `tasks.md` without GitHub Issue materialization
- Using `tasks.md` as the tracking SSOT after materialization
- Diverging re-generated `tasks.md` from existing GitHub Issues

After materialization, `tasks.md` is demoted to `archived derived artifact`. Tracking authority transfers to Issues / PRs.

`gh issue create` procedures and delivery-rollup parent editing belong to `docs/dev/workflow.md` / `docs/dev/github-ops.md`.

## Registry / Discovery Rules

When adding a new `docs/product/**` document, update `docs/dev/ssot-registry.md` in the same PR:

### Entry template

```yaml
- id: <kebab-case-id>
  path: docs/product/<filename>.md
  title: <Document Title>
  keywords: [<relevant, keywords>]
  description: <One-line description>
  sections:
    - "## <Key Section>"
```

### Directory mapping template

Add or extend the `docs/product/**` directory mapping in `ssot-registry.md`:

```yaml
- pattern: "docs/product/**"
  ssots:
    - docs/dev/product-spec-lifecycle.md
    - docs/product/requirements.md
    - docs/adr/0002-sdd-tool-adoption.md
```

### Routing Table entry

Add to `docs/dev/workflow.md` SSOT Routing Table:

```markdown
| product spec / docs/product/** ライフサイクル | `docs/dev/product-spec-lifecycle.md` |
```

## Loading Policy

This document follows `scoped_loading: required` from ADR 0002 token_policy.

**Load only when creating, updating, or archiving `docs/product/**` content.**

Do not add this document to unconditional auto-load lists in `CLAUDE.md` or `project-constitution.md`.
Conditional reference format:

```markdown
- `docs/product/**` を作成・更新・archive する場合のみ `docs/dev/product-spec-lifecycle.md` を読む。
```

## Verification Commands

```bash
# File exists
test -f docs/dev/product-spec-lifecycle.md && echo "PASS" || echo "FAIL"

# Responsibility boundary with workflow.md documented
rg -q "Responsibility Boundary|workflow\.md" docs/dev/product-spec-lifecycle.md && echo "PASS" || echo "FAIL"

# token_policy content present
rg -q "compact_spec|scoped_loading|diff-first|full_regeneration: prohibited|tasks\.md.*staging" \
  docs/dev/product-spec-lifecycle.md && echo "PASS" || echo "FAIL"

# tasks.md staging artifact -> GitHub Issue flow documented
rg -q "tasks\.md|staging.artifact|GitHub Issue" docs/dev/product-spec-lifecycle.md && echo "PASS" || echo "FAIL"

# Registry entry exists
rg -q "product-spec-lifecycle" docs/dev/ssot-registry.md && echo "PASS" || echo "FAIL"

# docs/product/** directory mapping exists
rg -q 'docs/product/\*\*' docs/dev/ssot-registry.md && echo "PASS" || echo "FAIL"

# workflow.md SSOT Routing Table entry exists
rg -q "product-spec-lifecycle" docs/dev/workflow.md && echo "PASS" || echo "FAIL"

# Conditional read-order entry exists
rg -q "product-spec-lifecycle" CLAUDE.md .claude/rules/project-constitution.md && echo "PASS" || echo "FAIL"
```
