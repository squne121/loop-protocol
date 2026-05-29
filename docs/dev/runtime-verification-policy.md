# 動作検証 AC 運用ポリシー

> SSOT: このファイルは動作検証 AC の設計・実装・検証・PR レビュー時に参照される唯一の正本。
> 関連 Issue: #84（policy 新規作成）、#77（適用判定スキーマ追加）、#83（SubAgent SKIP 検知実装）

---

## Runtime Verification Applicability

全 Issue は動作検証の適用判定（`not_applicable | immediate | deferred`）を明示する。

```markdown
## Runtime Verification Applicability

- decision: not_applicable | immediate | deferred
- reason: <判定理由>
- if immediate: 対応 AC / VC / 証跡要件
- if deferred: 後続 Issue / 統合フェーズ / 検証条件
```

| decision | 意味 |
|---|---|
| `not_applicable` | 静的検証（typecheck / lint / unit test / build）のみで完結し、プロセス起動・通信・I/O は不要 |
| `immediate` | 本 Issue の実装範囲内で動作検証が成立する（runtime AC / VC / 証跡 / SKIP exit 77 / fallback FAIL を要求する） |
| `deferred` | 複数機能の統合が前提で、後続 Issue / プレイアブルスライス / system test フェーズで初めて成立する（後続検証先を明示する） |

### ゲーム開発における deferred の例

単独 Issue の変更が「入力処理」「描画」「敵 AI」「当たり判定」「ゲームループ」などの一部分であり、単独での実動作検証が意味を持たない場合は `deferred` が適切。
ゲームテストは状態空間・相互作用が大きく、単独 Issue での網羅的な動作検証設計が困難なケースがある。

### decision ごとの要求事項

- `not_applicable`: runtime AC / VC / 証跡は不要。静的検証のみ。
- `immediate`: 動作検証 AC に `<!-- runtime-verification: true -->` タグを付与。VC に SKIP exit 77 / fallback FAIL の実装を含める。証跡を PR に添付する。
- `deferred`: 後続 Issue 番号・統合フェーズ名・検証条件を明記する。証跡は後続 Issue / フェーズで提出する。

### deferred 記述の必須フィールド

`decision: deferred` を宣言する場合、以下のフィールドをすべて記載することが必須。不完全な場合は `review-issue` の C10 blocker となる。

```markdown
## Runtime Verification Applicability

- decision: deferred
- reason: <deferred にする判定理由>
- deferred_destination:
    - destination_type: issue | phase | milestone
    - destination_ref: <Issue番号 / フェーズ名 / マイルストーン名>
- deferred_verification_condition: <検証が成立するために必要な条件の説明>
```

| フィールド | 型 | 説明 |
|---|---|---|
| `decision` | enum | `deferred` 固定 |
| `reason` | string | なぜ本 Issue では動作検証が成立しないかの理由 |
| `deferred_destination.destination_type` | `issue \| phase \| milestone` | 後続検証先の種別 |
| `deferred_destination.destination_ref` | string | Issue 番号（例: `#123`）/ フェーズ名 / マイルストーン名 |
| `deferred_verification_condition` | string | 後続の何が完了すれば動作検証が成立するかの条件説明 |

### decision と runtime-verification タグの整合ルール

以下の整合ルールを `issue-contract-review` および `review-issue` が検出する:

| 状態 | 判定 |
|---|---|
| `decision: immediate` かつ AC に `<!-- runtime-verification: true -->` タグが 1 つ以上ある | 整合（正常） |
| `decision: immediate` かつ `<!-- runtime-verification: true -->` タグが 1 つもない | **blocker**（タグ付与を要求） |
| `decision: not_applicable` または `decision: deferred` かつ `<!-- runtime-verification: true -->` タグがある | **blocker**（decision と矛盾。タグ削除または decision 変更を要求） |

---

## 1. 目的とスコープ

### 目的

SKIP exit 0 / `_fallback: true` 等による「検証の骨抜き」を防止し、動作検証 AC が実際に実行環境上で検証されることを保証する。

### スコープ

- Runtime Verification Applicability が `immediate` の Issue
- 動作検証 AC に対応する Verification Commands（VC）
- test-runner SubAgent による VC 実行と証跡保存
- pr-review-judge による PR レビュー判定
- implementation-worker による実装（VC スクリプトと artifacts/ 出力ロジックの実装まで）
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

issue-contract-review は `runtime-verification: true` タグを検出した場合、本ポリシーの SKIP 規約・証跡保存・Stop Condition 連動を適用するよう Issue 着手前に明示する（implementation-worker は VC スクリプトと artifacts/ 出力ロジックを実装、test-runner が実行と証跡保存を担う）。

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

### 証跡ディレクトリの運用設計

- `artifacts/` は **worktree-local の作業領域** とし、main ブランチに commit しない。
- 証跡の永続化先は PR 本文の `## Runtime Verification Evidence` セクション（次節）への inline 引用のみ。
- repo root の `.gitignore` に `artifacts/` を追加して track 対象外とする。実 `.gitignore` 編集は本ポリシーの Allowed Paths 外のため follow-up Issue で扱う（既存 #35 で tmp/ と合わせて追加する想定。未完了の場合は `git status` で `artifacts/` が untracked となっていることを実装者が手動確認する）。
- 既存ログは worktree 削除（post-merge-cleanup）と同時に消える。長期保存が必要な動作検証は PR 本文に inline 引用するか、GitHub Actions Artifacts などの別経路で扱う。

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

実行と保存は **test-runner SubAgent** の責務に集約する。実装者（implementation-worker）のコンテクストは実装作業と PR 起票に専念させ、VC 実行は test-runner が担う。

- **implementation-worker**: 動作検証 AC に対応する VC スクリプト（bash / pytest 等）を実装する。スクリプト内に「`artifacts/` ディレクトリを `mkdir -p` で作成し、上記フォーマットでログを書き出すロジック」を組み込む。実行は行わない。
- **test-runner**: VC スクリプトを実行し、スクリプトが artifacts/ に出力したログを後続 SubAgent（pr-review-judge）へ引き渡す。exit code・stdout・artifacts ログを統合した結果を `TEST_VERDICT_MACHINE` に乗せる。
- test-runner は `disallowedTools: Edit, Write, MultiEdit` のため、直接ファイル書き込みはせず、VC スクリプト自身が artifacts/ を書く設計とする（test-runner.md の「読み取り専用スクリプトのみ実行可」例外として `artifacts/` への append を許容するルール更新は #83 で実施）。
- 証跡の PR 引用は test-runner 結果を受けた implementation-worker または pr-reviewer が PR 本文 `## Runtime Verification Evidence` セクションへ inline 化する（次節参照）。

---

## 5. テストシナリオ最小セット

Runtime Verification Applicability が `immediate` の Issue は、以下の最小セットを満たすテストシナリオを VC に含める必要がある。

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

### ACP transport 動作検証スクリプト (`verify_acp_roundtrip.sh`)

ACP transport の end-to-end 動作検証は `.claude/skills/gemini-cli-headless-delegation/scripts/verify_acp_roundtrip.sh` で実施する。

このスクリプトは以下の 2 種類のシナリオを含む:

1. **scenario 1（実 Gemini CLI 向け正常系）**: PONG roundtrip — 実 Gemini CLI が pre-authenticated の場合のみ実行。`GEMINI_TELEMETRY_OUTFILE` に telemetry.json を出力し、`--debug` flag（`GEMINI_ACP_DEBUG=1`）で ACP JSON-RPC protocol ログを stderr に記録する。実 CLI 不在または auth 失敗の場合は SKIP exit 77 を返す。
2. **scenario 2（deterministic fake-agent 向け controlled experiment）**: permission proxy の branch が side effect を制御することを検証する決定論的テスト。実 Gemini CLI を使わないため常に実行される。

#### 実 Gemini CLI 向けの証跡取得フロー（AC1: Issue #113）

実 Gemini CLI が存在し pre-authenticated の場合、以下の環境変数が自動設定される:

| 環境変数 | 既定値 | 用途 |
|---|---|---|
| `GEMINI_ACP_DEBUG` | `1` | `gemini --acp --debug` を有効化し ACP JSON-RPC ログを stderr に記録 |
| `GEMINI_TELEMETRY_ENABLED` | `true` | gemini CLI の telemetry 出力を有効化 |
| `GEMINI_TELEMETRY_TARGET` | `local` | ローカルファイルへの telemetry 書き出し |
| `GEMINI_TELEMETRY_OUTFILE` | `artifacts/runtime-verification-AC7-<TIMESTAMP>.telemetry.json` | telemetry 出力先 |

呼び出し元が `GEMINI_TELEMETRY_ENABLED=false` をエクスポートすることで telemetry を無効化できる（CI 環境等）。

#### telemetry.json 証跡の PR 引用規約（AC4: Issue #113）

`artifacts/runtime-verification-AC7-<TIMESTAMP>.telemetry.json` を PR 本文 `## Runtime Verification Evidence` セクションに引用する際は、以下の **redact ルール** を適用する:

| 引用すべき内容 | 引用禁止（redact）する内容 |
|---|---|
| `gemini --version` / OS 情報 | HOME / token / OAuth identifier |
| コマンド形状: `gemini --acp --debug` | absolute local path（フルパス） |
| initialize / session/new / session/prompt が観測された事実 | prompt 全文 |
| scenario verdict（PASS / FAIL / SKIP） | telemetry.json 全文の貼り付け |

**禁止**: `telemetry.json` の full content を PR 本文に貼り付けること（token / OAuth 情報漏洩リスク）。
**必須**: 観測した ACP イベント種別の列挙（`initialize`, `session/new`, `session/prompt` 等）と verdict のみを引用する。

---

## 6. Stop Condition 連動

### 実行環境不可の検知

動作検証 AC の VC が exit code 77（SKIP）を返した場合、test-runner は以下のアクションを取る。

1. VC スクリプトが artifacts/ に書き出した SKIP ログを `TEST_VERDICT_MACHINE` に紐付ける
2. `stop_condition_triggered: true` を結果に含める
3. 呼び出し元（impl-review-loop / main session）に Stop Condition 発火を返却する

### エスカレーション手順

```
実行環境不可（SKIP exit 77）検知
  ↓
VC スクリプトが SKIP ログを artifacts/ に書き出す
  ↓
test-runner が exit code 77 を検知し stop_condition_triggered: true を返却
  ↓
impl-review-loop / main session が Stop Condition を発火
  ↓
人間担当者への通知（Issue コメント または PR コメント）
  ↓
実行環境の整備または Issue のスコープ変更を人間が判断
```

### Issue Stop Conditions への反映

Runtime Verification Applicability が `immediate` の Issue は `## Stop Conditions` に以下を追加する。

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

Runtime Verification Applicability が `immediate` の PR の本文には以下のセクションを追加する。

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
| **implementation-worker** | 動作検証 AC に対応する VC スクリプトを実装する（artifacts/ への書き出しロジックを含む）。VC の実行と証跡保存は test-runner に委ねる。実装者のコンテクストは実装作業と PR 起票に集中させ、SKIP / fallback の判定結果を受け取った後の Stop Condition 反応（PR コメントや Issue 更新）まで担う |
| **test-runner** | VC スクリプトを実行し、スクリプトが artifacts/ に書き出した証跡と exit code を統合した `TEST_VERDICT_MACHINE` を返す。exit code 77 を SKIP として認識し `stop_condition_triggered: true` を返す。fallback 検出時は FAIL として返す。実行と証跡集約はここに一元化する |
| **pr-review-judge** | PR 本文の `## Runtime Verification Evidence` セクションを確認。証跡がない・SKIP のみ・fallback PASS の場合はマージを BLOCK する。正常系・異常系の両シナリオ証跡が揃っているかを確認 |

---

## 9. 安全主張記述基準

> 関連 Issue: #137（Safety Claim Matrix 導入）

安全境界・権限・サンドボックス・transport・auth・MCP・native tools・approvalMode を扱う PR の安全主張は、以下の基準に従って記述する。

### 「閉じる経路を正確に書く」原則

安全主張は **実装が閉じている経路に限定して** 書く。未制御の範囲まで主張の射程を広げてはならない。

| 記述パターン | 判定 | 理由 |
|---|---|---|
| 「ACP client-side の fs/terminal proxy を提供しない」 | 許可 | 実装が閉じている経路（fs/terminal proxy の不提供）に限定されている |
| 「read-only ACP transport」 | **禁止** | Gemini CLI の native tool registry / settings 由来 MCP / approvalMode が未制御なのに transport 全体が read-only であるかのように誤読される |
| 「sandboxed execution」 | **禁止**（未制御範囲がある場合） | sandbox が何を囲んでいるかが不明瞭 |
| 「`clientCapabilities.fs=false` により ACP client-side fs proxy を無効化している」 | 許可 | 閉じる経路が具体的に明示されている |

### 安全主張の必須要素

安全主張を含む PR には以下を記載する:

1. **Claim**: 何を主張しているか（閉じた経路に限定）
2. **Implemented?**: 実装済みか（yes / no / partial）
3. **Not controlled**: 意図的に対象外にした範囲（未制御範囲を正直に列挙）
4. **Evidence**: 主張を裏付ける Verification Command の結果または linked issue の VC との対応
5. **Follow-up**: `Not controlled` が非空の場合の後続 Issue（必須）

### SAFETY_CLAIMS_V1 machine-readable schema

PR 本文の Safety Claim Matrix は以下の YAML 形式でも表現できる（`## Safety Claim Matrix` セクション内に埋め込む場合）。自動検証ツールはこの schema を参照して parse する。

```yaml
# SAFETY_CLAIMS_V1
safety_claims:
  - claim: "<閉じた経路に限定した安全主張の文字列>"
    implemented: "yes | partial | no"
    not_controlled:
      - "<意図的に対象外にした範囲の文字列>"   # 空の場合はリストを省略または []
    evidence:
      - "<Verification Command 文字列 または VC 結果 URL>"
    follow_up:
      - "#<Issue番号>"   # not_controlled が非空の場合は必須。空の場合は省略または []
```

| フィールド | 型 | 説明 |
|---|---|---|
| `claim` | string | 安全主張の内容（閉じた経路に限定すること） |
| `implemented` | `"yes" \| "partial" \| "no"` | 実装状態 |
| `not_controlled` | string[] | 未制御範囲の列挙（空の場合は `[]` または省略） |
| `evidence` | string[] | VC コマンド文字列または結果リンク（1 件以上必須） |
| `follow_up` | string[] | `not_controlled` 非空の場合は `#N` 形式の Issue 番号が 1 件以上必須 |

**制約:**
- `not_controlled` が非空の場合、`follow_up` に open Issue 番号が 1 件以上必要（`open-pr` スクリプトが検証）
- `implemented: "no"` の claim は PR 本文に含めず follow-up Issue に移動することを推奨

### 禁止表現（Not controlled が非空の場合）

`Not controlled` 列が非空の場合、以下の無限定な表現を PR title / summary / docs に使ってはならない:

- `safe`（無限定）
- `read-only`（無限定）
- `sandboxed`（無限定）
- `isolated`（無限定）
- `complete`（完全性の主張として使う場合）

これらを使う必要がある場合は、**主張の射程を明示的に限定** すること（例: 「ACP client-side fs/terminal proxy に対して read-only」）。

### PR レビューでの適用

`pr-review-judge` は Safety-sensitive PR（安全境界に関わる changed paths / diff keywords / linked issue text に基づいて判定）に対して Safety Claim Matrix の検査を行う。詳細は `.claude/skills/pr-review-judge/SKILL.md` の「Safety Claim Gate」を参照。

---

## 関連ドキュメント

- `docs/dev/session-recording-policy.md` — session 記録 Kill Switch policy（`session_recording_policy/v1` SSOT）。`secrets_mode` 遷移時の session 記録制御・Kill Switch 手順・checkpoint visibility 検証を定める
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill の全体的な責務境界
- `docs/dev/workflow.md` — 全体ワークフロー
- `.claude/skills/implement-issue/SKILL.md` — 実装手順
- `.claude/skills/pr-review-judge/SKILL.md` — Safety Claim Gate の詳細手順
- `.claude/agents/test-runner.md` — test-runner SubAgent 定義
- `.claude/agents/pr-review-judge.md` — pr-review-judge SubAgent 定義
- `.github/pull_request_template.md` — PR 本文テンプレート（Safety Claim Matrix セクション含む）
- Issue #77 — 動作検証 AC 必須化横断改善
- Issue #83 — SubAgent SKIP 検知責務の実装
- Issue #137 — Safety Claim Matrix 導入（PR #81 過大安全主張の再発防止）
