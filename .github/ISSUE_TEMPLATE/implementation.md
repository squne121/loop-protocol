---
name: Implementation
about: "`1 Issue = 1 PR` 前提で implementation child issue を起票する"
title: "実装: "
labels:
  - enhancement
  - phase/implementation
  - state/queued
  - agent/implementer
---

## Machine-Readable Contract

<!-- implementation issue では以下 5 キーを必須とする。 -->
```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#<parent-issue-number>|none"
goal_ref: "<親 Goal または単独改善の目的>"
change_kind: docs|code|mixed|workflow|data
```

## Parent Issue
- #

## Parent Goal Ref
- Goal:
- Desired Destination: <!-- parent desired only; this issue の write-capable scope ではない -->

## Current Validated Scope
- <!-- write-capable scope のみ -->

## Remaining Parent Gaps
- [ ]

## Outcome
<!-- 1 文で書く。完了したと判断できる状態を具体的に -->

## In Scope
-

## Out of Scope
-

## Acceptance Criteria
<!-- - [ ] 形式の検証可能な項目を列挙する -->
-

## Verification Commands

<!-- 必須セクション: 各 AC に対応するターミナルで実行可能なコマンドを列挙する。
     形式例:
       # AC1: <AC の内容>
       grep -n "キーワード" path/to/file
       # AC2: <AC の内容>
       pnpm test path/to/specific-test
     - 各コマンドは Terminal AI Agent が自己完結で実行・検証できるものにする。
     - 実行不可能なコマンド・存在しないファイルへの参照は含めない。
     - `src/` / `tests/` を含む Allowed Paths がある場合は以下 4 コマンドを必ず含める。 -->
- `pnpm typecheck` — TypeScript 型エラーなし
- `pnpm lint` — ESLint エラーなし
- `pnpm test` — Vitest 全件 PASS
- `pnpm build` — vite build 成功

## Allowed Paths
<!-- 編集可の具体パスを列挙する。assets/, LICENSES/ は AI 編集禁止 (CLAUDE.md) -->
-

## Stop Conditions

<!-- 必須セクション: 実装中にこれらの状況が発生したら直ちに作業を停止し、
     Issue comment に状況を記録して人間判断を待つ。 -->
- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約（キー集合・スキーマ・型定義）の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合（スコープ分割が発生する場合）
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Required Skills

<!-- 任意セクション: この Issue の実装が技術的に呼び出すスキル (runtime dependency) のみを記載する。
     issue-contract-review / implement-issue / pr-review-judge / ssot-discovery 等の標準ワークフロー
     スキルは暗黙的に必要なため列挙しない。runtime dependency がない場合は本セクションを省略してよい。 -->
- なし（runtime dependency なし）

## Scope Delta（該当時のみ記載）

> 任意。Allowed Paths と実作業の乖離がない場合は省略してよい。

| Stage | 変更内容 | 根拠（ユーザー指示 / 上位 Issue / 方針）|
|-------|---------|--------------------------------------|
| 1 | ... | ... |

## Delivery Rule
- `1 Issue = 1 PR` を厳守する
- worktree は `.claude/worktrees/<slug>/` に配置（リポジトリ外配置は禁止）
- 検証は Verification Commands 全 PASS を条件に PR を起票する
