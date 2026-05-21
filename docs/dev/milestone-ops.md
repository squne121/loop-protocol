# Milestone 運用規約（SSOT）

GitHub Milestone の作成・管理・クローズに関する単一の真実の情報源（SSOT）。
AI エージェント・人間レビュアー双方が参照する。

## SSOT 境界

### この文書が持つもの

- GitHub Milestone の責務定義（何を milestone として立てるか・立てないか）
- Milestone / Parent Issue / Implementation Issue / PR の責務分担
- Milestone 命名規則（title / description / due_on 方針）
- Milestone close 条件
- AI エージェント操作フロー / 人間 fallback フロー（RACI 含む）
- GitHub REST API エンドポイント参照
- Milestone 識別子規約（`number` vs `id`）

### この文書が持たないもの

- 個別 Issue のスコープ判断（各 Issue 本文・feature spec が正本）
- ラベル運用規約（`docs/dev/github-ops.md` が正本）
- PR レビュー・マージ手順（`docs/dev/workflow.md` が正本）
- ブランチ保護・権限設定（`docs/dev/github-ops.md` が正本）

---

## Milestone の責務

### Milestone として立てるもの

- **開発フェーズ（Phase）の区切り**: `M1: Foundation Gate (v0.1.x)` のような、複数の関連 Issue を束ねる里程標
- **リリース目標**: 外部・内部向けに「この機能群をここまでに届ける」という約束を持つ単位
- **品質ゲート**: CI / テストカバレッジ・セキュリティレビューなど、特定条件を満たすことでフェーズが完結する区切り

### Milestone として立てないもの

- 単一 Issue の進捗管理（Issue 自体がその役割を持つ）
- PR 単位の作業追跡（PR は Issue に紐づく成果物であり、Milestone に直接紐づけない）
- 個人タスクの期限管理（チーム・プロジェクト全体の節目でない場合）
- 実験的スパイク・調査系タスクのみで構成される一時的なバケット

---

## Milestone / Parent Issue / Implementation Issue / PR の責務分担

| 単位 | 責務 | 関係 |
|---|---|---|
| **Milestone** | 開発フェーズ・リリース目標の区切り。複数の parent issue を束ねる。close 条件は「割り当てた Issue がすべて close」または明示的な人間判断。 | Issue を `milestone` フィールドで紐づける |
| **Parent Issue** | 機能・テーマ単位の追跡。child implementation issues を束ねる。`closure_mode` に従って close する。 | Milestone に割り当てられる / child issues を持つ |
| **Implementation Issue** | 1 PR に対応する具体的な実装タスク。`## Allowed Paths` / `## Acceptance Criteria` を持つ。 | Parent Issue の sub-issue / Milestone に間接的に紐づく |
| **PR** | Implementation Issue の成果物。`Closes #N` で Issue を close する。Milestone には直接紐づけない。 | Implementation Issue を close する |

---

## 命名規則

### title

```
<Phase ID>: <フェーズ名> (<バージョン>)
```

例:
- `M1: Foundation Gate (v0.1.x)`
- `M2: Gameplay Core (v0.2.x)`
- `Q1-2026: Release Candidate`

- Phase ID は大文字英字 + 数字（`M1`, `M2`, `Q1-2026` 等）
- フェーズ名は人間可読な短い説明（20 文字以内を推奨）
- バージョン表記は `vX.Y.z` または `vX.Y.x`（patch 全体をまとめる場合は `x`）

### description

- 目標（Goal）を 1〜3 文で記述する
- 含める Issue / 除外する Issue の方針を明示する（任意）
- AI エージェントが参照できるよう英語または日本語どちらでも可

### due_on

- 外部コミットメント（リリース日・イベント日）がある場合のみ設定する
- 内部目標のみの場合は `null`（GitHub API では `due_on` フィールドを省略）
- 設定する場合は ISO 8601 形式（`YYYY-MM-DDTHH:MM:SSZ`、GitHub API は UTC）

---

## Close 条件

Milestone を close するには以下の条件をすべて満たすこと:

1. **割り当てた Issue がすべて close されている**  
   `gh api repos/{owner}/{repo}/milestones/{milestone_number}` の `open_issues` が `0` であること

2. **人間による意図的な判断がある**  
   Milestone の close は `scope` や `目標達成` の判断を含むため、AI エージェントが自動で close しない。  
   人間が `gh api --method PATCH repos/{owner}/{repo}/milestones/{milestone_number} -f state=closed` を実行するか、GitHub UI から close する

3. **Scope 変更なし、または明示的な scope 変更の記録がある**  
   含める / 外す Issue の変更があった場合、Milestone description または関連 ADR に記録する

### AI エージェントによる自動 close の禁止

Milestone の close は **人間の意思決定が必要** であり、AI エージェントは自動 close しない。  
`open_issues: 0` の検知は AI エージェントが rollup コメントで人間に通知するまでとする。

---

## AI エージェント操作フロー / 人間 fallback フロー

### RACI 定義

| 操作 | AI エージェント（R/A） | 人間（A/C） |
|---|---|---|
| Milestone 作成 | **R**: `gh api` で作成を実行 | **A**: 目標・scope・命名の最終承認 |
| Issue を Milestone に割り当て | **R**: `gh issue edit --milestone` で実行 | **C**: 割り当て方針の確認（例外時） |
| Milestone 進捗 readback | **R**: API で `open_issues` / `closed_issues` を取得・報告 | **C**: 内容確認 |
| 進捗 rollup コメント投稿 | **R**: Issue / PR にコメント投稿 | **I**: 通知受信 |
| Milestone close | **I**: close 可能条件を人間に通知 | **A**: 最終判断・close 実行 |
| Scope 変更（Issue の追加・除外） | **C**: 影響分析・提案 | **A**: 最終判断・実行 |
| 破壊的変更（milestone 削除・rename） | **I**: 変更の影響を報告 | **A**: 実行・承認 |

> R = Responsible（実行者）, A = Accountable（最終責任者）, C = Consulted（相談先）, I = Informed（通知先）

### AI エージェント操作フロー（通常時）

```
[1] Milestone 作成
    └─ gh api --method POST repos/{owner}/{repo}/milestones \
         -f title="<title>" \
         -f description="<description>" \
         [-f due_on="<ISO8601>"]
    └─ readback: 返却された number・id を記録

[2] Issue を Milestone に割り当て
    └─ gh issue edit {issue_number} --milestone {milestone_number} --repo {owner}/{repo}
    └─ readback: gh issue view {issue_number} --json milestone で確認

[3] 進捗 rollup
    └─ gh api repos/{owner}/{repo}/milestones/{milestone_number}
    └─ open_issues / closed_issues を取得
    └─ 関連 Issue にコメント投稿（github-ops.md の Body File Guidance に従う）

[4] open_issues: 0 を検知したら人間に通知
    └─ close は実行しない
```

### 人間 fallback フロー

以下のいずれかの場合、AI エージェントは操作を停止し人間にエスカレーションする:

| 条件 | 対応 |
|---|---|
| **権限不足**（403 / 404）| エラーを Issue コメントに記録し、人間に手動実行を依頼 |
| **silent drop**（API 呼び出しは 200 だが実際に反映されない）| readback で確認し、不一致を報告 |
| **SSOT 衝突**（milestone の割り当てが他ドキュメントの方針と矛盾する）| 自動解決せず、矛盾を Issue コメントに記録し人間判断を要求 |
| **Milestone close 判断** | AI は close せず、条件を満たしたことを通知するのみ |
| **Scope 変更（Issue 追加・除外）** | 提案のみ行い、人間の承認後に実行 |

### Human escalation コメントテンプレ

```markdown
## milestone-ops: Human Escalation Required (<timestamp>)

- Milestone: <title> (#<number>)
- 理由: <権限不足 / silent drop / SSOT 衝突 / scope 変更 / close 判断>
- 状況: <具体的なエラー・矛盾の内容>
- 依頼: <人間に実行してほしい操作>
```

---

## GitHub REST API エンドポイント参照

### Milestone CRUD

| 操作 | メソッド | エンドポイント |
|---|---|---|
| 一覧取得 | `GET` | `/repos/{owner}/{repo}/milestones` |
| 作成 | `POST` | `/repos/{owner}/{repo}/milestones` |
| 取得 | `GET` | `/repos/{owner}/{repo}/milestones/{milestone_number}` |
| 更新 | `PATCH` | `/repos/{owner}/{repo}/milestones/{milestone_number}` |
| 削除 | `DELETE` | `/repos/{owner}/{repo}/milestones/{milestone_number}` |

### Issue への Milestone 割り当て

| 操作 | メソッド | エンドポイント | パラメータ |
|---|---|---|---|
| Milestone 割り当て・変更 | `PATCH` | `/repos/{owner}/{repo}/issues/{issue_number}` | `milestone`: milestone の `number`（integer） |
| Milestone 解除 | `PATCH` | `/repos/{owner}/{repo}/issues/{issue_number}` | `milestone`: `null` |

### `gh` CLI 等価コマンド

```bash
# Milestone 作成
gh api --method POST repos/{owner}/{repo}/milestones \
  -f title="M1: Foundation Gate (v0.1.x)" \
  -f description="開発基盤・運用ルール・最小仕様正本を固めるフェーズ"

# Issue を Milestone に割り当て（gh issue edit 経由）
gh issue edit {issue_number} --milestone {milestone_number} --repo {owner}/{repo}

# Milestone 進捗確認
gh api repos/{owner}/{repo}/milestones/{milestone_number} \
  --jq '{title: .title, open: .open_issues, closed: .closed_issues, state: .state}'

# Milestone close
gh api --method PATCH repos/{owner}/{repo}/milestones/{milestone_number} \
  -f state=closed
```

---

## Milestone 識別子規約

GitHub の Milestone には 2 種類の識別子がある。

| 識別子 | フィールド名 | 値の例 | 用途 |
|---|---|---|---|
| **number** | `number` | `1`, `2`, `3` | REST API の path パラメータ。URL に含める識別子。`/repos/{owner}/{repo}/milestones/{number}` の `{number}` に使う |
| **id** | `id` | `12345678` | GitHub database identifier。GraphQL の `node_id` 相当。通常の REST 操作では使わない |

### 規約

- REST API path パラメータには必ず `number` を使う（`id` を path に使うと 404 になる）
- `gh api` / `curl` でエンドポイントを指定する際は `number` を path に埋め込む
- `id` は内部参照・GraphQL・webhook payload での識別に使われる場合があるが、REST path には使わない
- Milestone 作成直後の readback で `number` を取得・記録し、以後の操作に使用する

### readback で number を取得する例

```bash
MILESTONE_NUMBER=$(gh api --method POST repos/{owner}/{repo}/milestones \
  -f title="M1: Foundation Gate (v0.1.x)" \
  --jq '.number')
echo "Milestone number (REST path parameter): $MILESTONE_NUMBER"
```

---

## 関連ドキュメント

- `docs/dev/github-ops.md` — ラベル運用・認証・Body File Guidance・permissions 方針（SSOT）
- `docs/dev/workflow.md` — Issue 駆動開発フロー全体（SSOT）
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界
- `docs/dev/current-focus.md` — 現在のフェーズと優先順位
