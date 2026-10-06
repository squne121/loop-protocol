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

#### 予約 literal `post-merge-live-evidence`（merge 後の live evidence を待つ宣言、Issue #2878）

`destination_type: phase` かつ `destination_ref: post-merge-live-evidence` は、merge 後に canonical main root / default branch で取得する live evidence を待つ
`deferred` の予約 literal である。既存 enum（`issue | phase | milestone`）と必須 field は変更しない。この組を宣言した Issue は、PR reference の判定で
「merge で close してはならない」（A2。`Refs #N` が正しい reference）として扱われ、live evidence の取得・証跡リンク・残 AC の充足を確認した後に
operator / orchestrator が明示的に close する。判定規則の正本は `docs/dev/workflow.md` の「PR reference と Issue close の分離」で、evaluator は
`.claude/skills/open-pr/scripts/validate_pr_body.py` に 1 つだけ実装される。

```markdown
- decision: deferred
- deferred_destination:
    - destination_type: phase
    - destination_ref: post-merge-live-evidence
- deferred_verification_condition: <merge 後に実 production subprocess で取得する live evidence の条件>
```
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

### SKIP exit code: 77（SKIP を宣言する exit code）

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

**Issue #1856（evidence authority cutover, Phase 1）**: `TEST_VERDICT_MACHINE` は動作検証（Runtime Verification）の証跡集約フォーマットとして引き続き使用するが、通常レビュー（pr-review-judge / impl-review-loop Step 2）の APPROVE/REQUEST_CHANGES 判定に対しては non-authoritative（advisory）である。通常レビューの authoritative evidence は `CI_CHECK_RUN_SCOPED` と exact head SHA + literal command SHA256 に束縛された独立実行 Issue VC のみ（`.claude/skills/pr-review-judge/references/evidence-policy.md` 参照）。

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

### SAFETY_CLAIMS_V1 machine-readable schema（機械可読スキーマ）

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

## 10. worktree-agent-runtime-smoke（Claude Code／Codex CLI の runtime smoke を担う共有 Skill の説明）

Claude Code／Codex CLI の実 process／TUI を起動して runtime verification を行う Issue（例: hook
lifecycle、session 非永続化、worktree cwd binding の観測が必要な場合）は、`.claude/skills/worktree-agent-runtime-smoke/SKILL.md`
を経由する。

- **既定は structured lane**（非対話 `claude -p --output-format stream-json` / `codex exec --json --ephemeral`）で、
  stream JSON／JSONL event と exit code を証跡とする。TUI screen scraping は使わない。
- **interactive herdr lane** は TUI `/status`、Skill picker、approval 画面、subagent UI 等、structured lane で
  露出しない状態の観測が必要な場合だけ使用する。herdr 未検出・`HERDR_ENV` 未設定は `mode=interactive` の
  SKIP（exit 77）とし、structured lane の失敗へ波及させない。default interactive lane は人間の
  使用中 Herdr session に相乗りせず、ambient `HERDR_*` を strip した isolated named session を
  新規生成して own-name cleanup だけを行う。pre-existing/human namespace を enumerate、list、
  snapshot、operate、message、observe しない。既存の `--require-session-baseline-preservation` を
  明示した場合だけ before/after preservation observation を fail-closed で行い、通常実行では
  `preexisting_herdr_preserved` を `null` のままにする。
- Runner（`scripts/agent-ops/run_worktree_agent_runtime_smoke.py`）は runtime 起動・観測・証跡収集だけを所有し、
  hook reason の意味分類・mutation deny の妥当性・review verdict 等の semantic 判定は caller が引き続き所有する。
- Claude peer messaging を対象にする immediate verification は、harness-owned invocation-local
  `crossSessionInbound: "refuse"` と bare `SendMessage` / `ListAgents` deny の exact generated-settings
  test、および peer を開始・観測・送信先にしない controlled spawned-process canary を含める。
  configured evidence (`peer_policy_configured`, `cross_session_inbound_configured_refuse`) と observed
  evidence (`outbound_peer_tools_absent`, `agent_spawn_completion_observed`, `herdr_namespace_isolated`,
  `preexisting_herdr_preserved`) を混同しない。inbound behavior は configured value から推論せず、
  runtime unavailable / nested isolated-Herdr constraint は bounded reason-code SKIP exit 77 として
  artifact を残し、PASS に読み替えない。nested-session policy により fresh isolated launch が
  拒否された場合は `herdr_isolated_session_unavailable` を記録し、baseline-preservation
  opt-in を指定していない通常 lane は snapshot を試行しないため、snapshot unavailable を
  SKIP/FAIL の根拠にしない。
- **SubAgent 実行の causal evidence は hook ID 相関を要求し、marker 文字列出力のみでは不十分とする**（Issue #2183、
  Issue #2174 OWNER REQUEST_CHANGES https://github.com/squne121/loop-protocol/issues/2174#issuecomment-5302215173、
  PR #2214 OWNER レビュー https://github.com/squne121/loop-protocol/pull/2214#issuecomment-5307009937、
  PR #2220 レビュー fix-delta を踏まえた明文化）。完了 marker（`--forbid-marker` / 完了 marker 等）はモデル生成
  テキストの一部であり、synthetic な fixture でも trivially 満たせる自己申告に等しい。SubAgent 実行の PASS 判定
  は、`subagent_causal_evidence_verdict()`（`scripts/agent-ops/run_worktree_agent_runtime_smoke.py`）が計算する
  `causal_evidence_source` が `hook_id_correlated`（同一 `agent_id` を持つ `SubagentStart`/`SubagentStop` ペアが
  観測され、かつ `SubagentStop` payload の `agent_transcript_path` が観測できた場合のみ）であることを必要条件と
  する。marker 文字列の一致のみを根拠に PASS と判定してはならない（`marker_only_insufficient`／`no_evidence` は
  PASS の根拠にならない）。marker 文字列判定は補助情報として残してよいが、単独で PASS 判定に用いない。
  - **適用範囲（opt-in ではなく既定強制の範囲を明示する）**: structured lane（`run_structured_claude` 経由）は
    hook stream-json チャンネルが常に利用可能なため、呼び出し側が `--expect-marker` を指定する場合、この
    causal-evidence 要件は **既定で強制される**（追加フラグ不要）。`--require-subagent-causal-evidence` フラグは、
    `--expect-marker` を指定しない structured lane 実行にもこの要件を課したい場合にのみ必要となる。
  - **interactive lane の限定**: interactive lane（`run_interactive_herdr_isolated` 経由、herdr pane の
    テキストレンダリング）は `--include-hook-events` の stream-json hook payload を構造的に転記しないため、
    `causal_evidence_source` は現状 `no_evidence`／`marker_only_insufficient` に留まる構造的制約がある。この
    lane では本要件は引き続き **opt-in**（`--require-subagent-causal-evidence` を明示した場合のみ exit を
    昇格）であり、interactive-lane 向け hook 出力チャネルの整備は別途 follow-up とする。

## 11. Claude extension surface risk-trigger policy（拡張サーフェスの risk-trigger 判定）

> 関連 Issue: #2259（OWNER レビュー P0-7 で明示）、#2283（本セクション新設）、#2290（enforcement 配線 follow-up）

Claude extension surface（`.claude/agents/**` / `.claude/hooks/**` / `.claude/skills/**` /
`scripts/claude-gpt/**`）の変更が、静的な frontmatter・宣言の確認だけで足りるか、
実際の runtime 挙動確認（system test、`Runtime Verification Applicability: immediate`）を
要求するかを判定する versioned machine-readable rule set を
`docs/dev/extension-surface-runtime-policy.yaml` として新設する。

### 正本の所在

`docs/dev/extension-surface-runtime-policy.yaml` が構造化正本（`schema_version` /
`resolution` / `unknown_surface_policy` / `assumptions[]` / `verification_profiles` /
`rules[]` を持つ YAML）であり、本セクションはその説明・表示用に留める。rule 本体の
スキーマ変更・追加・改廃は同 YAML ファイルを直接編集する。`schema_version` の
構造は `docs/dev/extension-surface-runtime-policy.schema.json`（JSON Schema,
draft 2020-12）が closed validator として検証し、
`docs/dev/tests/test_extension_surface_runtime_policy_schema.py` の pytest で
`jsonschema.validate()` により機械検証する。

### rule set の概要

各 rule は以下のフィールドを持つ:

| フィールド | 説明 |
|---|---|
| `id` | rule 識別子 |
| `selectors[]` | `source_scope`（`project` / `user` / `managed` / `plugin` / `session` / `cli`）ごとに `component` と、`project` は repository 相対 `path_globs`、それ以外は `runtime_resolved_only: true` + `evidence_source` を持つ構造 |
| `semantic_detectors[]` | 型付き検出条件。`type`（`yaml_frontmatter_keys` / `markdown_body` / `json_config_keys` / `script_body_semantics`）と対象 `keys`/`classification`、対象 `change_types`（`add`/`modify`/`delete`/`rename`）を持つ |
| `execution_context` | 実行文脈（例: `subagent_delegation`、`hook_concurrent_execution`） |
| `default_decision` | 既定の Runtime Verification Applicability decision（通常 `immediate`） |
| `verification_profile` | top-level `verification_profiles` object 内の profile ID への参照。参照先 profile は `runner` / `mode` / rule 固有の `assertions[]`（証明すべき postcondition）を持つ |
| `exceptions[]` | 証明契約付きの例外。`predicate`（`proven_not_runtime_loaded` 等）に加え `evaluation_mode: human_evidence_required` / `default_when_unproven: not_applied` / `required_evidence[]` / `evidence_freshness: current_head` / `approval_authority: owner` を持つ |
| `last_verified` | rule 単位の最終確認日 |
| `assumption_refs` | top-level `assumptions[]` の `claim_id` への参照（任意） |

トップレベルの `resolution` は複数 rule への同時一致時の合成方針
（`multiple_matches: evaluate_all` / `final_decision: most_restrictive` /
`exception_scope: per_rule`）を定義する。`min_claude_code_version` は rule 単位
ではなく、トップレベル `assumptions[]`（`claim_id` 単位で
`min_claude_code_version` / `last_verified` / `applicability` を持つ）で管理する。

`docs/dev/extension-surface-runtime-policy.yaml` は agent / hook / skill /
claude-gpt lifecycle、および SubAgent の start/stop・delegation・fallback
semantics のそれぞれに対応する rule を最低 1 件ずつ定義する。

### 例外は証明可能な predicate として定義する

`docs-only` のようなファイル種別ベースの粗い例外は採用しない。以下のような
runtime 非読込・非配布・意味不変を証明できる predicate 形式のみを例外として
認め、各 predicate は `evaluation_mode: human_evidence_required` /
`default_when_unproven: not_applied`（証明不足時は例外を非適用とする） /
`required_evidence[]` / `evidence_freshness: current_head` /
`approval_authority: owner` を伴う evidence-backed contract として定義する:

- `proven_not_runtime_loaded`: 現行 production 経路から到達不能であることの証明
- `proven_not_distributed`: 配布・有効化経路が存在しないことの証明
- `production_consumer_inventory_empty`: 呼び出し元が repo 全体に存在しないことの証明
- `executable_or_prompt_semantics_unchanged`: 実行可能セマンティクス・prompt 意味が
  変更前後で同一であることの証明

### unknown surface の扱い

上記 4 surface のいずれにも一致しない変更（未知の extension surface）は
`not_applicable` へ黙って倒さない。`docs/dev/extension-surface-runtime-policy.yaml`
の `unknown_surface_policy` は `decision: human_judgment`（人間判断への
エスカレーション） / `gate: block`（人間判断が確定するまで進行を止める） /
`override_requires`（block を解除できる権限）を持つ構造として定義する。

### 既存 decision enum との関係

本 rule set は `## Runtime Verification Applicability` の既存 decision enum
（`not_applicable` / `immediate` / `deferred`。本ドキュメント冒頭セクション参照）を
再定義しない。各 rule の `default_decision` は「この surface / semantic_delta に
該当する変更は `immediate` の根拠として扱われるべきである」という判定材料を
補強するものであり、decision enum の値・意味そのものを変更するものではない。

### 根拠となる Claude Code 公式ドキュメント

- Claude Code Auto mode では SubAgent frontmatter の `permissionMode` が無視され、
  親 session の classifier が child tool call にも適用される
  （https://code.claude.com/docs/en/sub-agents）。
- 複数 hook が同一イベントに一致した場合、全 hook が並行実行され、いずれかが
  deny を返しても sibling hook の副作用は停止しない
  （https://code.claude.com/docs/en/hooks-guide）。

### enforcement 配線は別 Issue

本セクションおよび `docs/dev/extension-surface-runtime-policy.yaml` は
policy definition のみを scope とする。`review-issue` / `issue-contract-review` /
PR diff gate への実際の deterministic gate 配線は follow-up Issue #2290
（実装: Claude extension surface risk-trigger policy を review-issue /
issue-contract-review の deterministic gate に配線する）の責務であり、
#2290 が完了するまで本セクションの追加をもって「機械的に強制済み」とは
扱わない。

### profile の assertion binding における完全性の確認（Issue #2771）

`matched_rules[].enforcement == "hard"` のみから到達した各 `verification_profile`
の `assertions[]` 全件は candidate assertion であり、Issue の
`## Runtime Verification Applicability` に `runtime_assertion_bindings` として
それぞれの扱い（`disposition`、後述）を明示しなければならない。
`enforcement == "advisory"` のみから到達した profile / assertion は
この required set に一切含まれない（PR #2370 の advisory non-blocking 方針を
継続する）。

`scripts/agent-guards/extension_surface_policy_matcher.py` の
`derive_required_runtime_assertions()` / `evaluate_runtime_assertion_binding_
coverage()` が、composite identity `(verification_profile_id, assertion_id)` を
基準に required set と宣言された binding set の missing / unknown / duplicate
（1 key = 1 binding。同一 key の複数回宣言は disposition の組合せ・bind 先 `ac` の
異同を問わず duplicate）を決定論的に検証し、`dispositive` の各 binding の `ac` に
ついて実在確認・`applicable_acs` 包含・decision レベルの runtime-verification
タグ整合・`## Verification Commands` の `# AC<N>` canonical reference 存在を
確認する。`review-issue`（`check_issue_contract.py` の C15）と
`issue-contract-review`（`contract_readiness_check.py`）は同一のこの shared
evaluator を呼ぶため、両者は drift しない。

本 gate が保証するのは binding の **structural completeness**（宣言漏れ・未知
宣言・重複宣言の不在、binding 先 AC の referential integrity）のみであり、binding
された VC が assertion の意味を実際に証明しているか（**semantic sufficiency**）は
判定しない。判定しない旨は shared evaluator のコード中にも明示する
（`structural_completeness_not_semantic_sufficiency`）。policy 自身の欠陥
（dangling profile reference、同一 profile 内の assertion ID 重複）は Issue の
`needs_fix` に偽装せず、policy integrity failure として区別する。author 向けの
記法と生成規則は `.claude/skills/create-issue/references/body-
authoring.md` の「実行時検証プロファイルの assertion binding 記法」を参照する。

#### assertion 単位の適用可否の宣言（`disposition`、Issue #2852）

各 `runtime_assertion_bindings` entry は閉じた enum の `disposition` で、policy が
選んだ candidate assertion を今回の変更でどう扱うかを宣言する。disposition ごとの
key 集合は閉じており、集合外の key を持つ entry は malformed として拒否される。

| disposition | 意味 | key 集合 |
|---|---|---|
| `dispositive` | 今回の runtime AC が検証する | `profile` / `assertion` / `ac` / `disposition` |
| `non_dispositive_readiness_compat` | 別の**既存** AC が substantive verification を所有する | `profile` / `assertion` / `disposition` / `demonstrated_by` / `reason`（`ac` は持たない） |
| `not_applicable` | 今回の change surface に対象の振る舞い自体が存在しない | `profile` / `assertion` / `disposition` / `reason`（`ac` / `demonstrated_by` は持たない） |

- `disposition` を省略した `profile` / `assertion` / `ac` の旧形式は読み取り互換として
  受理され、`dispositive` 相当・`disposition_source: legacy_default` として扱う。
  明示した場合は `disposition_source: explicit`。いずれも「実証済み」を意味しない。
- `demonstrated_by` は実在する既存 AC の参照（`AC<N>`）のみ。当該 AC は Acceptance
  Criteria に実在し、`## Verification Commands` の canonical `# AC<N>` 参照を持つ
  必要がある。`applicable_acs` / runtime-verification タグへの追加は要求しない。
  test path / pytest node-id の resolver は持たない。
- `reason` の機械検証は非空文字列のみ（言語判定などの gate は追加しない）。
- `not_applicable` / compat は自身の required key を「宣言済み」にするが、他の required
  assertion の missing、および unknown / duplicate / malformed / invalid の判定を隠さない。
  required set に無い key への `not_applicable` は unknown になる。
- evaluator の結果は分類済み binding 一覧と disposition 別の assertion 一覧を返すが、
  runtime PASS 根拠を表す field は一切持たない。分類は `review-issue` /
  `issue-contract-review` の成功経路でも非 blocking の carrier
  （`runtime_assertion_disposition_classification`）として公開され、
  `merged_review_result` まで保持される。carrier は verdict / `overall_status` を変えない。
- `evaluate_issue_risk_trigger()` は assertion 単位の宣言の影響を受けない。hard-required の
  全 assertion が `not_applicable` / compat であっても、policy 由来の Issue-level の
  `decision` 要求は緩まない（保守的な既知の limitation。rule-level の緩和設計は
  policy 側の Issue #2775 の責務）。

不変条件:

- structural checker の PASS は runtime verification の PASS ではない。
- `dispositive` の宣言は実行済み / 観測済みを意味しない。
- `non_dispositive_readiness_compat` は runtime PASS ではない。
- `not_applicable` は runtime PASS ではなく、成功ではなく対象外の宣言である。
- applicability の分類は semantic sufficiency の証明ではない（`reason` / `demonstrated_by`
  の妥当性は既存 semantic review の責務で、新しい judge / 言語判定 gate は追加しない）。
- 対象の振る舞いが今回の変更に関係するのに未観測の assertion は unverified であり、
  `not_applicable` でも PASS でもない。「runner が無い」「認証できない」「test が未実装」は
  `not_applicable` の理由にならない。
- skipped / 未実行の必須 scenario が aggregate pass になる問題は別 Issue（#2841）の
  責務であり、本節は「正当な `not_applicable`」と「applicable だが未実行」を混同しない
  分類境界のみを定める。

---

## 12. live runtime verification における接続先 server 診断と実 request 受理の evidence 分離（Issue #2803 / #2938）

### `CLAUDE_GPT_HOME` は補助 binary の導入先であり credential namespace ではない

Issue #2925 で `scripts/claude-gpt/launch.sh` は upstream の minimal client contract へ縮退した。現行 launcher は `HOME` / `XDG_*` / `CLAUDE_CONFIG_DIR` を隔離せず、Native Claude Code と同じ ambient な user / project config をそのまま使う。

本節は `CLAUDE_GPT_HOME` に関する次の 2 つの semantics を区別する。launcher semantics は `scripts/claude-gpt` 配下の `launch.sh` / `repair_proxy.sh` / `runtime_smoke_test.sh` が `CLAUDE_GPT_HOME` に関して示す挙動の総称であり、runtime-VC fixture semantics は、runtime VC の hermetic repair fixture のために §14 の approval carrier が検証する条件である。

- launcher semantics（導出）: normal launcher（`launch.sh`）は、`CLAUDE_GPT_HOME` から `HOME` / `XDG_*` / `CLAUDE_CONFIG_DIR` を導出も上書きもしない。`CLAUDE_GPT_HOME` は Claude の config root の authority ではない。
- launcher semantics（継承）: caller が環境に設定済みの `CLAUDE_GPT_HOME` は ambient environment として子 process に継承され得る。`lib.sh` が設定する既定値 `${HOME}/.claude-gpt` は非 export の shell default であり、launcher が新たに export する値ではない。「config root の authority として使わない」ことと「環境変数が子 process に存在しない」ことは別の主張であり、混同しない。
- launcher semantics（repair）: `repair_proxy.sh` は、`CLAUDE_CODE_PROXY_INSTALL_DIR` が未指定のときだけ `$CLAUDE_GPT_HOME/bin` を default の install dir にする。
- launcher semantics（smoke）: `runtime_smoke_test.sh` は `CLAUDE_GPT_HOME` を work dir / evidence path の選択に使わない。work dir は `mktemp -d` で作られ、証跡は `scripts/claude-gpt/.evidence/` または `--evidence-out` が指す path に置かれる。
- runtime-VC fixture semantics: runtime VC の command / caller の環境が、`CLAUDE_GPT_HOME` を worktree 配下の `artifacts/runtime-smoke/` 以下の fixture home に設定する。approval carrier（`scripts/agent-ops/runtime_vc_approval_contract.py`）は、override 変数（`CLAUDE_CODE_PROXY_INSTALL_DIR` / `CLAUDE_CODE_PROXY_VERSION`）の未設定を要求し、caller が与えた `CLAUDE_GPT_HOME` が許可された fixture home の条件（絶対 path、`..` を含まない、symlink でない、`artifacts/runtime-smoke/` の厳密に配下）を満たすことを検証し、検証済みの同じ値を子 process の環境へ渡す。carrier は fixture home の path を生成も選択もしない。その結果、`repair_proxy.sh` の default（`$CLAUDE_GPT_HOME/bin`）は検証済みの fixture home 配下に解決される。carrier は `repair_proxy.sh` の default を置き換えず、その入力を検証済みの値に制約するだけである。したがって carrier は launcher semantics とは別の独立した機構ではない。詳細は §14.3 を参照する。

`CLAUDE_GPT_HOME` は認証済み credential を保持する namespace ではない。したがって `CLAUDE_GPT_HOME` の値を launcher と揃えること、または別の値を指定することは、認証状態の検証対象を変える操作にならない。

ChatGPT subscription の認証は、接続先 proxy server の所有者の責務である。`scripts/claude-gpt/preflight.sh` も `launch.sh --check-only` も認証状態を判定せず、認証状態や認証不足を表す field も出力しない。

### 2 種類の evidence と attribution を混同しない

次の evidence はそれぞれ独立した観測対象であり、いずれか単独を「runtime / entitlement PASS」と呼んではならない:

1. **接続先 server の診断**（`launch.sh --check-only` の `CLAUDE_GPT_LAUNCH_RESULT_V1`、および `runtime_smoke_test.sh` の `CLAUDE_GPT_SMOKE_RESULT_V1.launch_check_only` が持つ `connected_server`）: `ANTHROPIC_BASE_URL` が実際に向く running server への到達性（`reachable`）、`/v1/models` の HTTP status、required model の欠落（`missing_models`）、`model_catalog_ok`、`classification` だけを示す。`model_catalog_ok` は `/v1/models` に required model が列挙されていることだけを意味し、実 ChatGPT subscription request が受理されたこと、認証が有効であること、request ごとの routing / provider fallback の不在のいずれも証明しない。接続先 server の version / hash は現行 interface から観測できないため「未確認」と記録する。PATH 上の binary の情報（`local_proxy_binary_auxiliary`）は接続先 server が動かしている binary とは限らない非 authority の補助診断である。
2. **configured connected-server への実 request の受理**（先行 Issue #2772 の AC10 相当）: 接続先 server の診断とは別に、`ANTHROPIC_BASE_URL` が向く server が実 request を受理したことを、`runtime_smoke_test.sh` の各 step の完了などで評価する。これは「接続先 server が request を受理した」ことだけを示す。`runtime_smoke_test.sh` の成功だけでは、ChatGPT subscription / Codex provider がその request を受理したという attribution は成立しない。generic / custom な `ANTHROPIC_BASE_URL` でも同じ smoke shape は成立しうるためであり、request ごとの routing は `CLAUDE_GPT_PROXY_LOG` を明示した場合にだけ観測され、通常は未観測である。
3. **provider / subscription attribution**（別 evidence）: 実 ChatGPT subscription request が Codex provider に受理されたという attribution は、上記 2 とは別の evidence である。attribution への昇格は、canonical environment で server / provider の ownership が別 evidence により束縛された場合、または #2925 AC7 の operational trial などで provider / routing の evidence が得られた場合に限る。それ以外は「attribution 未確認」と記録する。

upstream（raine/claude-code-proxy の公式 docs）の契約も踏まえる。`ANTHROPIC_AUTH_TOKEN=unused` は Claude Code の client credential 要件を満たすだけで、upstream の認証には使われない。provider credentials と routing は proxy server 側（`CCP_*` や `config.json`）の責務である。`/healthz` は provider credentials や upstream の可用性を検証せず、`/v1/models` は catalog discovery であり、`/v1/messages` の `model` が provider を選ぶ。proxy は Codex 以外の provider も route しうる。

`connected_server` の診断が `ok`（`model_catalog_ok: true`）でも、それだけで実 request の受理や attribution を主張してはならない。逆に診断が `failed`（例: `reason: model_alias_not_resolved` / `cause: connected_server_model_catalog_incomplete`、exit 7）の場合は接続先 server の catalog 不整合であり、ChatGPT アカウントの entitlement や再認証の要否を示すものではない。この区別を保ったまま 3 種類の evidence を個別に評価すること。

---

## 14. 承認が必要な runtime VC の approval carrier（Issue #2839）

runtime VC が Auto mode の classifier に拒否される操作（例: repository 管理下の fixture installer を実行する repair command）を含む場合、対話 session で operator が承認しても、runner（`scripts/agent-ops/run_worktree_agent_runtime_smoke.py`）が起動する独立した `claude -p` にはその承認が届かない。本節は、この承認を子 session へ invocation 単位で bounded かつ auditable に渡す仕組み（approval carrier）と、それを渡せない契約を事前に検出する手順を定める。

### 14.1 承認は継承されない（前提）

親 transcript および親 session の承認は、独立した子 `claude -p` session に継承されない。classifier が読むのは、その session 自身の user message と実行される command だけである。したがって runtime VC の契約、docs、authoring guidance のいずれにも「対話 session で承認済みだから子でも通る」という前提を置いてはならない。承認が必要な runtime VC は、14.3 の carrier を使うか、14.5 の checker で `non_executable` と扱う。

### 14.2 ownership decision（採用と不採用の理由）

- **採用**: runner の closed enum `--approval-profile` による profile carrier。runner は既に settings overlay の唯一の producer であり、caller が settings を渡す入口を持たない。profile の registry、overlay の生成、precondition の検証、子 env の構築は `scripts/agent-ops/runtime_vc_approval_contract.py` に一箇所だけ定義し、runner はそれを読み込んで使う。
- **不採用（consumer wrapper）**: 新しい wrapper script は settings を渡す面を増やし、generic passthrough に退化しやすい。
- **不採用（caller 指定の `--settings`）**: caller が任意の JSON を渡せるため generic passthrough になる。
- **不採用（`~/.claude/settings.json` の恒久変更）**: user 全体の persistent setting を既定解にしない。project の `.claude/settings*.json` は `autoMode` として読まれない（公式仕様）ため使えない。

carrier が与える authority の出所は、repository でレビューされた registry 定数と、Issue の宣言（14.4）と、それらを含む PR の review である。live な operator 承認を runner が検証する仕組みは作らない。

### 14.3 carrier の仕様

- profile は closed enum で、初期値は `repair_proxy_hermetic_fixture` の 1 件だけである。
- 固定 overlay は、runner の基底 overlay 定数（hooks と `permissions.deny`）に `autoMode.allow` として `"$defaults"` と固定 rule 1 件を足したものである。broad allow は含めず、`soft_deny` / `hard_deny` / `environment` の key は overlay に含めない。
- `CLAUDE_GPT_HOME` の扱いは §12 の runtime-VC fixture semantics に従う。caller（runtime VC の command / 環境）が fixture home を `CLAUDE_GPT_HOME` に設定し、carrier はその値が許可された fixture home の条件を満たすことを検証した上で、検証済みの同じ値を子 process の環境へ渡す。carrier は fixture home の path を生成も選択もせず、launcher semantics の default（`repair_proxy.sh` の `$CLAUDE_GPT_HOME/bin`）も置き換えない。
- 固定 rule の文言は実装に即して次の内容を述べる。永続的な install 先は、検証済みの `CLAUDE_GPT_HOME` が指す fixture home（worktree 配下の `artifacts/runtime-smoke/` 以下）に限定される。repair の処理中は、ローカルの一時 file と一時 directory を作成して削除し、install 後にローカルで検証する。installer の取得元は repository 管理下の fixture（`file://`）であり、network installer は使わない。実際の user 領域の claude-gpt home は変更しない。「すべての書き込みが fixture home 配下に限られる」とは述べない。rule は module 定数であり、caller 由来の値は補間されない。
- carrier は native adapter かつ structured mode 専用である。`--require-hook-chain-evidence` とは併用できる。runner が実際に選択した固定 observation overlay（hook-chain 用の `PreToolUse` / `Stop` を含む）の同じ object に carrier の `autoMode` を足し、子 session へ渡す `--settings` は 1 個だけとする。`SubagentStart` / `SubagentStop`、hook-chain の `PreToolUse` / `Stop`、`permissions.deny`、`crossSessionInbound`、`--setting-sources project` はそのまま保持される。選択された overlay に `autoMode` だけを足したもの以外の overlay は、子の起動前に拒否される。
- 上記の合成は `--require-hook-chain-evidence` に限った狭い規則であり、汎用の overlay 合成 framework ではない。`--expect-skill-command`（overlay の派生 variant を使う）と `--hermetic-agent-definition`（別の settings file を使う）とは引き続き併用できず、合成規則を定義しないため fail-closed とする。
- runner は子 session の起動前に、fixture installer の実体（regular file、symlink 不可、worktree 配下、git 追跡済み、未コミット変更なし）、`CLAUDE_GPT_REPAIR_INSTALLER_URL` の完全一致、`CLAUDE_GPT_HOME` の配置（worktree 配下の `artifacts/runtime-smoke/` 以下、symlink と `..` による逸脱なし）、override 変数の未設定を決定論的に検証し、不成立なら子 session を起動せず fail-closed で終了する。
- 子 session の env は、検証済みの値で runner が明示的に組み立てて渡す。
- audit として、evidence に `approval_carrier`（`profile_id`、`repo_head`、`overlay_sha256`、`fixture_git_blob_hash`、`fixture_unchanged`、検証済み precondition の要約）を記録する。flag を使わない run の argv と evidence は変わらない。

carrier の実 classifier に対する効果は、classifier の run-to-run variance に依存するため、この節の hermetic pytest では検証しない。実 Auto mode での効果は、carrier を使う VC（例: Issue #2810 の AC9/AC10）の再実行で確認する。

### 14.4 宣言 grammar

承認を必要とする runtime VC を持つ Issue は、`## Runtime Verification Applicability` 内に 1 行 `approval_required_actions: [<profile_id>, ...]` を宣言し、対応する VC の runner command 行に `--approval-profile <profile_id>` を付ける。宣言が無いことは「承認不要の宣言」を意味する。VC と AC の紐づけは、VC ブロック内でコマンド行の直前にある `# AC<n>` コメント行で表す。

### 14.5 non-executable 検出（checker）

`scripts/agent-ops/runtime_vc_approval_contract.py` は Issue 本文を静的に照合し、`executable` / `non_executable` / `not_applicable` を返す。手動で実行する場合は次のとおり。

```bash
uv run --locked python3 scripts/agent-ops/runtime_vc_approval_contract.py --issue-body-file <file>
```

carrier の適用は Issue 全体ではなく、VC の runner invocation（runner 行）ごとに判定する。承認に関係する invocation とは、signature（`CLAUDE_GPT_REPAIR_INSTALLER_URL=` の env prefix）を持つ、または `--approval-profile` を持つ runner 行である。承認に関係しない runner 行には、この checker 独自の shell 書式制限を課さない。片方の VC にだけ carrier が付いていても、別の承認対象 VC の付け忘れは検出される。

command 行の解析は、サポートする小さな shell grammar に限った quote-aware な字句解析で行う（`shlex` の全面的な shell 解釈には依存しない）。引用符の種別を保持するため、二重引用符内および bare の `$PWD` は展開として受け付け、単引用符内および backslash escape の `$PWD` は literal（展開されない）として扱う。したがって `CLAUDE_GPT_HOME="$PWD/artifacts/..."` は有効だが、`CLAUDE_GPT_HOME='$PWD/artifacts/...'` は有効な fixture home として扱わない。`--claude-adapter "claude-gpt"` や `--mode "interactive"` のように引用符付きで書かれた flag 値も、引用符を外した値として判定する。`--expect-marker "A && B"` のように引用符内にある `&&` などは command 連結ではない。

判定は次の順に評価し、最初に該当した行で確定する。行 (4a)・(6a)・(7a) は、承認が届かない、または run 時に拒否される契約を `executable` と誤判定しないための補完である。

| 行 | 条件 | 結果 |
|---|---|---|
| 0 | 承認に関係する runner 行に解釈できない構文（引用符の外の `;`・`&&`・`\|`・`>`・`<`、`bash -c`、`eval`、command substitution、`$PWD` 以外の変数展開など）がある | `non_executable` |
| 1 | 宣言が無く、flag も承認が必要な signature（`repair_proxy.sh` 実行、`CLAUDE_GPT_REPAIR_INSTALLER_URL=` の env prefix）も無い | `not_applicable` |
| 2 | 宣言が無いが flag または signature がある | `non_executable` |
| 3 | 宣言が空・重複・未知の id を含む | `non_executable` |
| 4 | 宣言された id に対応する runner 行が無い | `non_executable` |
| 4a | signature を持つ runner 行に `--approval-profile` が無い（carrier の付け忘れ） | `non_executable` |
| 5 | flag を持つ runner 行が AC に紐づかない | `non_executable` |
| 6 | flag が `--claude-adapter claude-gpt` と併用されている | `non_executable` |
| 6a | flag が structured 以外の `--mode` と併用されている | `non_executable` |
| 7 | flag があるのに必須の env prefix が欠落または不一致 | `non_executable` |
| 7a | flag の値が宣言に無い、または registry に無い | `non_executable` |
| 8 | 上記のいずれにも該当しない | `executable` |

checker は現時点でどの gate にも自動では結線されていない。`approval_required_actions` を宣言する Issue の refinement で、operator または refinement loop がこの手順を手動で実行する。`baseline_vc_preflight.py` などへの結線は、それらを所有する Issue の merge 後に別途判断する。

## 15. Runtime Observation: permission_mode（SubagentStop の payload から観測する値の扱い、Issue #2854）

`scripts/agent-ops/run_worktree_agent_runtime_smoke.py` の `--require-observed-runtime-field permission_mode` が、何を観測済みとして扱うかを定める。`permission_mode` 以外の field（`effective_permission_profile` / `loaded_skill` / `executor` / `mutation`）には native 由来の extractor が無く、従来どおり unsupported のままである。

### 抽出元 event / field

観測として採用するのは、native stream-json 上の次の event だけである。

- event: `type == "system"` かつ `subtype == "hook_response"` かつ `hook_event == "SubagentStop"`
- field: その event の hook stdin payload（runner の固定 `--settings` overlay にある `command: "cat"` hook が `stdout` / `output` に echo した JSON）の `permission_mode`。取得経路は `extract_claude_hook_lifecycle_events()` と同じである。
- 値: 文字列で、`default` / `plan` / `acceptEdits` / `auto` / `dontAsk` / `bypassPermissions` のいずれか（公式 hooks reference の列挙）
- 候補の適格性: `stdout` / `output` の各 channel を、前後に文章の付かない純粋な JSON object 文字列として厳密に decode する（native の `cat` echo は純粋な JSON object である）。decode した 1 つの object の中で、`hook_event_name == "SubagentStop"`、非空の文字列 `agent_id`、列挙内の `permission_mode` が揃って初めて候補になる。`stdout` の不足を `output` で補完するような、channel をまたぐ field の合成はしない。hook handler の通常ログ（`Worker said: {...}` のような前置き付き出力）、非 JSON、`hook_event_name` が `SubagentStop` でない object は候補にならず、conflict でもなく無視する。
- 整合: 候補が得られた後に、同一 event の `stdout` / `output` の候補で値と `agent_id` が一致すること、採用できる観測が複数ある場合は値がすべて一致すること。

### 証明する範囲と証明しない範囲

- 証明する: native runtime が、特定の `SubagentStop` hook event の payload で報告した permission mode の値。
- 証明しない: 対象操作（repair 等）が実際に実行されたこと、deny / hook / permission の評価結果が実際に発生したこと、allow / deny / ask rule を含む完全な effective permission profile、evidence と対象 session / SubAgent / invocation の対応の全体。
- runner は `--permission-mode` を claude CLI へ渡さない。観測される値は Claude Code の既定値または設定に由来し、親 session の mode と同一とは限らない。
- `effective_permission_profile` は unsupported のままである。`permission_mode` の観測を `effective_permission_profile` の観測、alias、代替として扱ってはならない。

### 採用しない入力と fail-closed 規約

次は観測として採用せず、`permission_mode` を unavailable（exit 77、SKIP）にする。

- assistant / user / result event の本文や tool 出力に現れた同名 JSON
- `SubagentStop` 以外の hook event、`subtype` が `hook_response` でない event
- settings の再読込、`--agent-type` 等の static declaration、worker の自己申告
- 文字列以外の型、列挙外の値（native 形の候補が持つ場合は `invalid_or_conflicting_value`）、`agent_id` が空の payload
- 前置き文章付きの JSON、`hook_event_name` が `SubagentStop` でない payload、field が channel に分散している payload（各 channel 単独では候補にならない）
- `stdout` と `output` で値が異なる event、複数の `SubagentStop` event 間で値が矛盾する場合

fail-closed 規約: 抽出できなかった場合に推測・補完・fake fixture・declaration で PASS にしない。SKIP への変更は「unavailable が空でない」かつ元の exit code が 0 の場合に限り、runtime 失敗（非 0 終了、causal evidence gate の FAIL 等）の exit code は上書きしない。`--require-observed-runtime-field` 指定時だけ `summary.md` に `observed_runtime_fields` と `unavailable_required_runtime_observation_reasons` を記録する。理由は `permission_mode` が `no_subagentstop_hook_event` / `field_absent` / `invalid_or_conflicting_value`、未対応 field が `no_native_extractor` である。

### unconfirmed の記録規則

実 Claude Code の smoke で `permission_mode` を観測できなかった場合（exit 77）、その Claude Code version / mode / 起動経路では permission_mode は `unconfirmed` として記録する。`unconfirmed` は PASS ではなく、実装修正の完了でも #2810 の達成でもない。version・起動条件・exit code・`unavailable_required_runtime_observation_reasons` を PR 本文の Runtime Verification Evidence に引用し、推測や declaration で補完せず停止する。

### 観測できなかった場合の 5 区分の切り分け

| 区分 | 判定の目安 |
|---|---|
| runtime 起動失敗 | claude の起動・認証・timeout 等で runtime 自体が失敗している（exit 1 または別の SKIP 理由）。`permission_mode` の観測可否は判定できない |
| 対象 hook event 不発火 | `SubagentStop` の `system/hook_response` event が stream に無い（reason: `no_subagentstop_hook_event`） |
| event はあるが field 無し | `SubagentStop` event はあるが payload に `permission_mode` が無い（reason: `field_absent`） |
| field はあるが parser 不成立 | payload に `permission_mode` はあるが、native 形の候補で型不正・列挙外・channel / event 間の矛盾がある（reason: `invalid_or_conflicting_value`）。`agent_id` 欠落・`hook_event_name` 不一致・前置き付き出力は候補にならず無視するため `field_absent` になる。raw stream の値と parser の判定を突き合わせる |
| parser と観測が一致 | 採用基準を満たす値が `observed_runtime_fields` に記録され、raw stream の値と一致する |

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
