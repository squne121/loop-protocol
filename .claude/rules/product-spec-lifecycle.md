---
name: product-spec-lifecycle
description: docs/product/** を操作するときに自動適用される path-scoped rule。product-spec-lifecycle 手順を入口にする。
paths:
  - "docs/product/**"
---

# Product Spec Lifecycle — path-scoped rule

このルールは `docs/product/**` を変更する前に自動適用される。

## 適用トリガー

`docs/product/**` を以下のいずれかの操作で変更する場合、**このルールが優先して適用される**:

| 操作 | 説明 |
|---|---|
| `create` | 新規 spec / top-level doc の追加 |
| `update` | 既存 spec の diff-first 更新 |
| `archive` | spec のライフサイクル終端 (archived / superseded) |
| `tasks.md conversion` | tasks.md staging artifact → GitHub Issue 変換 |

## scoped loading 指示

操作開始前に以下を **この順番で** ロードする（scoped loading）:

1. `.claude/skills/product-spec-lifecycle/SKILL.md` — 操作手順の入口
2. `docs/dev/product-spec-lifecycle.md` — lifecycle 正本（状態遷移・token_policy・EARS・archive rules）
3. `docs/adr/0002-sdd-tool-adoption.md` — SDD ツール採否・generated_artifact_boundary（必要節のみ）

全文を無条件にロードしない（`scoped_loading: required`）。

## 操作種別ごとの scoped loading 指示

### create（新規作成）

- `SKILL.md` §Create を読む
- `docs/dev/product-spec-lifecycle.md` §Creation Rules を読む
- `docs/dev/ssot-registry.md` を読む（registry entry 追加が必要なため）
- `docs/dev/workflow.md` の SSOT Routing Table を読む（routing entry 追加が必要なため）

### update（差分更新）

- `SKILL.md` §Update を読む
- `docs/dev/product-spec-lifecycle.md` §Update Rules を読む
- `diff-first` — 全文再生成禁止（`full_regeneration: prohibited`）

### archive（アーカイブ）

- `SKILL.md` §Archive を読む
- `docs/dev/product-spec-lifecycle.md` §Archive rules を読む
- `superseded_by` / `archived_reason` / `archived_date` フィールド必須

### tasks.md conversion（GitHub Issue 変換）

- `SKILL.md` §Tasks.md Materialization を読む
- `docs/dev/product-spec-lifecycle.md` §tasks.md Adapter を読む
- materialization 完了後、`tasks.md` は derived artifact として降格する

## 禁止事項

- `full_regeneration`: doc 全文再生成禁止
- `delete_file`: ファイル削除禁止（traceability 保全のため archived 状態へ遷移させる）
- `.specify/` 由来 artifact を `docs/product/**` SSOT として扱うことを禁止
- `tasks.md` を GitHub Issue 変換前の tracking SSOT として使うことを禁止

## 参照先

- 手順 skill: `.claude/skills/product-spec-lifecycle/SKILL.md`
- lifecycle 正本: `docs/dev/product-spec-lifecycle.md`
- SDD ツール方針: `docs/adr/0002-sdd-tool-adoption.md`
- SSOT カタログ: `docs/dev/ssot-registry.md`
