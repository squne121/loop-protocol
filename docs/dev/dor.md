---
doc_id: DOC-DEV-DOR-001
title: Definition of Ready (DoR) — Implementation Issue
status: active
created_by_issue: 72
parent_issue: 40
---

# Definition of Ready (DoR) — Implementation Issue

Implementation Issue の DoR（着手可能条件）を明文化する。
本ドキュメントは AI Agent が「着手してよいか」を判定するための基準の正本定義である。

判定の実行は `issue-contract-review` skill が担う。本ドキュメントはその判定基準の SSOT であり、判定 logic（コード）は含まない（後続 follow-up Issue のスコープ）。

## 目的と位置づけ

DoR とは「AI Agent が安全かつ確定論的に実装を開始できる状態」の定義である。DoR を満たさない Issue を実装に渡すと、スコープ肥大・AC 未検証・衝突リスクなどが発生する。

本基準は以下の 3 層で構成される:

1. **構造的完備性**: 必須セクションが存在すること（機械判定可能）
2. **内容の確定論的検証可能性**: AC・VC が決定論的に判定できること（機械判定可能）
3. **スコープ妥当性と体験判断**: 粒度・体験・設計判断（人間判断が必要）

## Outcome

### 機械判定可能な条件

- `## Outcome` セクションが存在する
- Outcome が 1〜3 文で記述されている（空欄でない）

### 人間判断が必要な条件

- Outcome が 1 つの明確な成果に絞られているか（複数目的が混在していないか）
- Outcome がプレイヤー可視・運用可視な成果として表現されているか（実装手順の記述でないか）

## Acceptance Criteria

### 機械判定可能な条件

- `## Acceptance Criteria` セクションが存在する
- AC が `- [ ] AC<N>:` 形式のチェックボックスで記述されている
- AC が 1 件以上存在する
- 各 AC に対応する VC が `# AC<N>` コメントで番号一致している

### 人間判断が必要な条件

- AC 数が 3〜5 件程度に収まっているか（過多は粒度過大の兆候）
- AC に主観表現（「適切に」「品質を改善する」等）が混入していないか
- AC がそれぞれ独立した検証単位になっているか

## Verification Commands

### 機械判定可能な条件

- `## Verification Commands` セクションが存在する
- コマンドが 1 つ以上存在する
- 各コマンドが `grep` / `test -f` / `pnpm test` / `exit code` 等の決定論的判定を使っている
- 実装前 baseline で fail することが確認できる（VC preflight）

### 人間判断が必要な条件

- VC が AC をすべてカバーしているか（漏れがないか）
- VC の失敗が「実装不足」ではなく「VC 設計ミス」に起因しないか

## Allowed Paths

### 機械判定可能な条件

- `## Allowed Paths` セクションが存在する
- 1 件以上のパスが列挙されている
- 他の OPEN Issue との Allowed Paths 重複がない（worktree conflict チェック）

### 人間判断が必要な条件

- 列挙されたパスがスコープを適切に限定しているか（広すぎ・狭すぎがないか）
- `assets/` / `LICENSES/` などの保護領域が含まれていないか

## Stop Conditions

### 機械判定可能な条件

- `## Stop Conditions` セクションが存在する（implementation Issue のみ必須）
- 6 定型項目（Allowed Paths 外の変更 / 固定契約変更 / 新規 Issue 必要 / 後続 Phase 波及 / nested delegation / 外部サービス利用）が埋まっている

### 人間判断が必要な条件

- Stop Conditions がプロジェクト固有リスクを正しく列挙しているか
- 過剰な Stop Conditions が実装の摩擦になっていないか

## Required Skills

### 機械判定可能な条件

- `## Required Skills` セクションが存在する
- 内容が空欄（なし）または具体的な skill 名の列挙である

### 人間判断が必要な条件

- `issue-contract-review` / `implement-issue` 等の標準ワークフロースキルは「暗黙適用」扱いであり、Required Skills に列挙する必要はない（列挙されていても誤りではない）
- 列挙されたスキルが実際に必要か（不要な依存が着手ブロックになっていないか）

## issue-contract-review との対応

以下の表は、本 DoR の各項目が `issue-contract-review` skill の確認チェック（C1–C8 相当）のどれに対応するかを示す。

| DoR 項目 | issue-contract-review 確認項目 | 判定種別 |
|---|---|---|
| Outcome | テンプレ準拠（必須セクション存在） | 機械判定 |
| Acceptance Criteria | AC が `- [ ]` 形式 / AC ⇔ VC 番号一致 / 意味的評価混入なし | 機械判定 |
| Verification Commands | VC 明示 / VC preflight（baseline fail 確認） / 検証スクリプト型 | 機械判定 |
| Allowed Paths | Allowed Paths 明示 / worktree・branch 命名 preflight | 機械判定 |
| Stop Conditions | Stop Conditions 明示（6 定型項目） | 機械判定 |
| Required Skills | テンプレ準拠（必須セクション存在） | 機械判定 |
| スコープ妥当性・体験判断 | （本 skill の責務外 — `review-issue` / 人間レビュー） | 人間判断 |

## Out of Scope

- DoR 判定 logic の実装（本ドキュメントは判定基準の定義のみ。実装は A1 完了後の follow-up Issue で扱う）
- `state/ready` ラベル運用との連携（A2 のスコープ）
- Plan Mode スキップ条件（A3 のスコープ）

## 参照・引用

以下の情報源を参考に本ドキュメントを作成した。

- ChatGPT 回答（Issue #40 issuecomment-4489127733 / 引用日: 2026-05-19）
  - https://github.com/squne121/loop-protocol/issues/40#issuecomment-4489127733
  - 「Implementation Issue の Definition of Ready」項目の原型（Section 7.3）を参照
  - 「AIエージェントへの作業契約として Issue を機能させる設計」の根拠として引用
- GitHub Docs — Best practices for Projects（引用日: 2026-05-19）
  - https://docs.github.com/en/issues/planning-and-tracking-with-projects/learning-about-projects/best-practices-for-projects
  - 「Issue を分割し、依存関係を明確にし、Milestone / Label で追跡する」推奨と本 DoR の構造を照合
- GitHub Docs — About coding agents（引用日: 2026-05-19）
  - https://docs.github.com/copilot/concepts/agents/coding-agent/about-coding-agent
  - 現代の AI coding agent 運用では Issue がエージェントへの作業入力として扱われるという文脈で参照
- Anthropic Engineering Blog — Equipping agents for the real world with Agent Skills（引用日: 2026-05-19）
  - https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
  - Skill は「簡潔・構造化・実利用でテスト済みであるべき」という設計思想の根拠として参照
