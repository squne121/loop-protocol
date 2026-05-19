# 動作検証 AC 運用ポリシー

> SSOT: このファイルは動作検証 AC の設計・実装・検証・PR レビュー時に参照される唯一の正本。
> 関連 Issue: #84（policy 新規作成）、#77（動作検証 AC 必須化）、#83（SubAgent SKIP 検知実装）

---

## 1. 目的とスコープ

### 目的

SKIP exit 0 / `_fallback: true` 等による「検証の骨抜き」を防止し、動作検証 AC が実際に実行環境上で検証されることを保証する。

### スコープ

- 動作検証 AC（`runtime-verification: true` タグを持つ AC）を含む全 Issue
- 動作検証 AC に対応する Verification Commands（VC）
- test-runner SubAgent による VC 実行
- pr-review-judge による PR レビュー判定
- implementation-worker による実装と証跡保存
- issue-contract-review による動作検証 AC の設計審査

### スコープ外

- 静的検証のみの AC（typecheck / lint / unit test のみで検証できるもの）
- pnpm typecheck / lint / build / test の標準 4 コマンドのみで完結する検証

---

## 2. 「動作検証 AC」の定義

### 定義

「動作検証 AC」とは、テスト環境または実際の実行環境上でプロセスを起動・通信・I/O させることで初めて検証できる Acceptance Criterion を指す。静的な型検査・lint・ユニットテストでは代替できない。

### 静的検証との区別

| 種別 | 判定方法 | 例 |
|------|---------|-----|
| 静的検証 AC | pnpm typecheck / lint / test / build | 型エラーなし、テスト全件 PASS |
| 動作検証 AC | プロセス実行・通信・I/O を必要とする | ACP transport 疎通、WebSocket メッセージ到達、外部 API レスポンス |

### メタタグ `runtime-verification: true` の使い方

Issue の `## Acceptance Criteria` で動作検証 AC には以下のタグを付与する。

```markdown
- [ ] AC7: ACP transport が正常終了し response を返す <!-- runtime-verification: true -->
```

issue-contract-review は `runtime-verification: true` タグを検出した場合、本ポリシーの SKIP 規約・証跡保存・Stop Condition 連動を適用するよう implementation-worker に指示する。

---

## 3. SKIP 規約

動作検証 AC を実行不可能な環境（外部サービス未起動・権限不足・ネットワーク遮断等）では SKIP を宣言し、実行環境不可として Stop Condition を発火させる。SKIP は「検証をパスしたこと」を意味しない。

### SKIP exit code: 77

VC スクリプトが SKIP を宣言する場合、exit code **77** を返す。

```bash
# 実行環境が不可の場合の SKIP 宣言例
if ! command -v some-service &>/dev/null; then
  echo "SKIP: some-service が見つかりません。実行環境を確認してください。"
  exit 77
fi
```

### stdout `SKIP:` プレフィックス

SKIP 宣言時は stdout の先頭に `SKIP:` プレフィックスを付けた説明メッセージを出力する。

```
SKIP: 外部サービス未起動のため AC7 の動作検証を実行できません。
```

### `_*_fallback: true` JSON フィールド

動作検証の結果が JSON 形式で返される場合、`_*_fallback: true` フィールド（例: `_transport_fallback: true`、`_connection_fallback: true`）が含まれる場合は fallback 経由であるため **FAIL** として扱う。PASS と判定してはならない。

```bash
# fallback 検出例
RESULT=$(some-command --json)
if echo "$RESULT" | grep -q '"_.*_fallback": *true'; then
  echo "FAIL: fallback が発火しています。実際の通信経路を確認してください。"
  exit 1
fi
```

### SKIP vs FAIL の判別

| 状況 | 判定 | exit code |
|------|------|-----------|
| 実行環境が整っており検証が成功した | PASS | 0 |
| 実行環境が整っており検証が失敗した | FAIL | 1 |
| 実行環境が不可（サービス未起動・権限不足等）| SKIP | 77 |
| fallback 経由で「成功」した | FAIL | 1 |

---

## 4. 証跡保存フォーマット

### 保存先パス

```
<worktree>/artifacts/runtime-verification-<AC>-<timestamp>.log
```

例: `.claude/worktrees/issue-84-runtime-verification-policy/artifacts/runtime-verification-AC7-20260520T120000Z.log`

### ログの必須フィールド

```
=== Runtime Verification Log ===
AC: <AC番号と内容>
Timestamp: <ISO 8601 UTC>
Environment: <OS / 実行ランタイム / 関連サービスのバージョン>

--- Input ---
<VC に渡したコマンド・引数・環境変数（機密値はマスク）>

--- Output ---
<stdout / stderr の全文、または最大 500 行>

--- Verdict ---
Result: PASS | FAIL | SKIP
Exit Code: <数値>
Reason: <判定理由（FAIL/SKIP の場合は必須）>
```

### 証跡保存の責務

- implementation-worker が動作検証 AC の VC を実行し、上記フォーマットで artifacts/ に保存する。
- artifacts/ は `.gitignore` に登録されている場合でも PR 本文にログ内容を引用する（次セクション参照）。
- artifacts/ ディレクトリが存在しない場合は mkdir -p で作成する。

---

## 5. テストシナリオ最小セット

動作検証 AC を含む Issue は、以下の最小セットを満たすテストシナリオを VC に含める必要がある。

### 必須: 正常系 ≥ 1

実行環境が整っている状態で、期待する通信・I/O・応答が得られるシナリオ。

```bash
# 正常系の例（ACP transport）
# 前提: ACP サーバーが localhost:8080 で起動済み
RESPONSE=$(curl -sf http://localhost:8080/health)
echo "PASS: 正常系 - ACP health check: $RESPONSE"
```

### 必須: 異常系 ≥ 1

意図的にエラー条件を与え、エラーハンドリングが正しく動作するシナリオ。

```bash
# 異常系の例（ACP transport - permission deny）
RESPONSE=$(curl -sf -H "Authorization: invalid-token" http://localhost:8080/api 2>&1)
if echo "$RESPONSE" | grep -q "401\|403\|Unauthorized\|Forbidden"; then
  echo "PASS: 異常系 - 不正トークンが正しく拒否されました"
else
  echo "FAIL: 異常系 - 不正トークンが拒否されていません: $RESPONSE"
  exit 1
fi
```

### ACP transport の例シナリオ

| シナリオ | 種別 | 確認内容 |
|---------|------|---------|
| 正常なメッセージ送受信 | 正常系 | response が期待値と一致 |
| permission deny（不正トークン）| 異常系 | 401/403 が返る |
| timeout（サーバー無応答）| 異常系 | timeout エラーが返り fallback が発火しない |
| fallback 発火 | 異常系 | `_*_fallback: true` → FAIL 判定 |

---

## 6. Stop Condition 連動

### 実行環境不可の検知

動作検証 AC の VC が exit code 77（SKIP）を返した場合、test-runner は以下のアクションを取る。

1. artifacts/ に SKIP ログを保存する
2. `stop_condition_triggered: true` を結果に含める
3. implementation-worker にエスカレーションを通知する

### エスカレーション手順

```
実行環境不可（SKIP exit 77）検知
  ↓
test-runner が SKIP ログを artifacts/ に保存
  ↓
implementation-worker が Stop Condition を発火
  ↓
人間担当者への通知（Issue コメント または PR コメント）
  ↓
実行環境の整備または Issue のスコープ変更を人間が判断
```

### Issue Stop Conditions への反映

動作検証 AC を含む Issue は `## Stop Conditions` に以下を追加する。

```markdown
## Stop Conditions

- 動作検証 AC の VC が SKIP（exit 77）を返した場合（実行環境不可のためエスカレーション）
- fallback 経由でのみ「成功」する場合（FAIL として扱い、実行環境を確認）
```

### pr-review-judge の Stop Condition 連動

pr-review-judge は PR の証跡セクションを確認し、以下のいずれかが検出された場合はマージを BLOCK する。

- VC が SKIP（exit 77）で終了した証跡のみが存在する
- `_*_fallback: true` を含む結果が PASS として引用されている
- 動作検証 AC に対応する証跡が PR 本文に存在しない

---

## 7. PR 本文への証跡引用テンプレ

動作検証 AC を含む PR の本文には以下のセクションを追加する。

```markdown
## Runtime Verification Evidence

### AC<N>: <AC 内容>

- Result: PASS | FAIL | SKIP
- Timestamp: <ISO 8601 UTC>
- Environment: <OS / ランタイム>
- Exit Code: <数値>

<details>
<summary>実行ログ（クリックで展開）</summary>

\`\`\`
<artifacts/runtime-verification-AC<N>-<timestamp>.log の内容>
\`\`\`

</details>

<!-- fallback なし確認: _*_fallback: true フィールドが含まれていないことを確認 -->
```

### 引用の必須事項

- PASS の場合: 正常系・異常系の両方のシナリオ結果を引用する
- FAIL の場合: エラー内容と原因調査の結果を引用する
- SKIP の場合: SKIP 理由と実行環境の状態を引用する（Stop Condition 発火済みであることを明記）

---

## 8. 責務マッピング表

| 役割 | 動作検証 AC に関する責務 |
|------|------------------------|
| **issue-contract-review** | 動作検証 AC の設計審査。`runtime-verification: true` タグの存在確認。SKIP 規約・証跡保存・Stop Condition 連動が Issue に記載されているかを確認。実行環境依存の AC が Out of Scope に含まれていないかを確認 |
| **implementation-worker** | 動作検証 AC の VC を実行し、artifacts/ に証跡を保存する。SKIP（exit 77）を検知した場合は Stop Condition を発火し人間にエスカレーション。fallback を PASS とみなさない |
| **test-runner** | implementation-worker から委譲された VC を実行し結果を返す。exit code 77 を SKIP として認識し `stop_condition_triggered: true` を返す。fallback 検出時は FAIL として返す |
| **pr-review-judge** | PR 本文の `## Runtime Verification Evidence` セクションを確認。証跡がない・SKIP のみ・fallback PASS の場合はマージを BLOCK する。正常系・異常系の両シナリオ証跡が揃っているかを確認 |

---

## 関連ドキュメント

- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill の全体的な責務境界
- `docs/dev/workflow.md` — 全体ワークフロー
- `.claude/skills/implement-issue/SKILL.md` — 実装手順
- `.claude/agents/test-runner.md` — test-runner SubAgent 定義
- `.claude/agents/pr-review-judge.md` — pr-review-judge SubAgent 定義
- Issue #77 — 動作検証 AC 必須化横断改善
- Issue #83 — SubAgent SKIP 検知責務の実装
