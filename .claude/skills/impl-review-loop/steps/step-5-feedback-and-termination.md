### Step 5: 判定（オーケストラレータ）

以下の終了条件を確認する（詳細は `Loop Termination` セクション参照）。

### Step 5 補助条件（`LOCAL_CI_RESULT`）

- `LOCAL_CI_RESULT` が `local-ci/just-check` として PR head SHA に紐づいているかを最終確認する。
- Commit Status が missing / non-success / `head_sha` 不一致なら、`LOOP_VERDICT` 生成前に `Step 2` と `Step 4` を再実行し、`reviewed_head_sha` と PR head SHA の一致条件を復旧する。

**LOOP_VERDICT 自動読み取り（P-2）:**

Step 4 完了後、以下のコマンドで LOOP_VERDICT を自動読み取りする。`verdict` と `blockers` の評価は**同一のコメント**に対して行うこと（2つの別クエリでは異なるコメントを拾う可能性がある）。`yaml.safe_load` で YAML を安全にパースし、文字列照合ではなく構造化データとして判定する:

> **パイプ + ヒアドキュメント禁止（Issue #1223）**: `gh ... | python3 - <<'PYEOF'` のように、パイプと bash ヒアドキュメントを同時に使う形式は**使用しないこと**。bash のリダイレクト優先度により、ヒアドキュメント（`<<'PYEOF'`）が python3 の stdin として解釈され、パイプ経由の `gh` 出力が読み取られず `body = ""` になる。必ず `python3 -c '...'`（インライン文字列）またはファイル経由パターンを使うこと。

```bash
# LOOP_VERDICT の自動読み取り（最新コメントから YAML を安全にパースして評価）
# reviews と comments の両方を結合して検索する（gh pr review --comment は reviews に格納されるため）
# "## LOOP_VERDICT" セクションヘッダーで絞り込む（"LOOP_VERDICT" の部分一致では他コメントの誤検出が起きる）
# 注意: `python3 -c '...'` 形式を使うこと。`python3 - <<'PYEOF'` はパイプ経由データが空になるため使用禁止。
gh pr view <PR番号> --json reviews,comments --jq '
  ([.reviews[] | {body, createdAt: .submittedAt}] + [.comments[] | {body, createdAt}])
  | map(select(.body | contains("## LOOP_VERDICT")))
  | sort_by(.createdAt)
  | .[-1].body
' | python3 -c '
import re
import sys

import yaml

body = sys.stdin.read()
match = re.search(r"## LOOP_VERDICT\s*```yaml\s*(.*?)\s*```", body, re.S)
if not match:
    print("false")
    raise SystemExit(0)

try:
    data = yaml.safe_load(match.group(1))
except yaml.YAMLError:
    print("false")
    raise SystemExit(0)

if not isinstance(data, dict):
    print("false")
    raise SystemExit(0)

ok = data.get("verdict") == "APPROVE" and data.get("blockers") == []
print("true" if ok else "false")
'
```
上記コマンドが `true` を返す場合にのみ終了条件を満たすとみなす。

**ファイル経由パターン（`python3 -c` が使いづらい場合の代替）**:

`python3 -c '...'` の引用符エスケープが複雑になる場合は、ファイル経由パターンを使うこと（PR #1222 で実施した回避策）:
```bash
# ファイルに保存してから読む（ヒアドキュメント + パイプの問題を回避）
mkdir -p tmp
gh pr view <PR番号> --json reviews,comments --jq '
  ([.reviews[] | {body, createdAt: .submittedAt}] + [.comments[] | {body, createdAt}])
  | map(select(.body | contains("## LOOP_VERDICT")))
  | sort_by(.createdAt)
  | .[-1].body
' > tmp/loop_verdict_raw.txt
python3 << 'PYEOF'
import re
import sys
import yaml

with open('tmp/loop_verdict_raw.txt') as f:
    body = f.read()
match = re.search(r"## LOOP_VERDICT\s*```yaml\s*(.*?)\s*```", body, re.S)
if not match:
    print("false")
    raise SystemExit(0)
try:
    data = yaml.safe_load(match.group(1))
except yaml.YAMLError:
    print("false")
    raise SystemExit(0)
if not isinstance(data, dict):
    print("false")
    raise SystemExit(0)
ok = data.get("verdict") == "APPROVE" and data.get("blockers") == []
print("true" if ok else "false")
PYEOF
```
このパターンでは `gh` 出力を `tmp/loop_verdict_raw.txt` に書き出してから python3 で `open()` 読み取りするため、stdin のパイプ競合が発生しない。

> **注意1**: 2つの別クエリで `verdict` と `blockers` を個別に評価すると、一方が最新コメント・他方が古いコメントを参照してしまう可能性がある。必ず同一コメントに対して両条件を同時評価する上記の統合クエリを使うこと。
> **注意2**: `gh pr review --comment` で投稿された verdict は `--json comments` ではなく `--json reviews` に格納される。`--json reviews,comments` で両方を結合して検索すること。
> **注意3**: `contains("LOOP_VERDICT")` ではなく `contains("## LOOP_VERDICT")` を使うこと。Verification Commands の grep 出力など他のコメントが "LOOP_VERDICT" という文字列を含む場合に誤検出が起きる。
> **注意**: reviews と comments を結合した際は時系列ソートが必要です。投稿順序によっては最新コメントが異なるため、sort_by(.createdAt) で統一的に最後のコメントを抽出すること。

**MEDIUM 指摘の判断ガイド（P-4）:**

adversarial-reviewer から MEDIUM 指摘がある場合、オーケストラレータは以下の判断フレームを適用する:

| 判断 | 条件 | アクション |
|---|---|---|
| **対応する** | Issue Scope 内の修正で解消可能 | Step 1 へフィードバックして修正させる |
| **Out of Scope** | Issue Allowed Paths 外または将来対応で十分 | その旨を明示してフィードバックに含めず次へ進む |
| **保留** | 実装への影響が不明 | pr-reviewer の判断に委ねる（Step 4 へ進む）。必ず下記の記録を残すこと |

**「保留」選択時の必須記録事項:**

LOOP_STATE コメントに以下を記録してから Step 4 へ進む:
- どの MEDIUM 指摘を保留しているか（adversarial-reviewer の指摘内容）
- 保留理由（影響範囲が不明、要調査、判断根拠）

pr-reviewer SubAgent への渡し情報に以下を必ず含める:
- 保留中の MEDIUM 指摘のサマリー
- 「この MEDIUM は live 検証または実装上のリスク評価が必要であるため、pr-reviewer が判断してください」

保留を解消できなかった場合（pr-reviewer も MEDIUM について判断を留保した場合）: 次の iteration の feedback として引き継ぎ、オーケストラレータが明示的に「継続対応」または「Out of Scope として棄却」を決定すること。

> **重要**: MEDIUM を理由に自動的にループを継続しないこと。オーケストラレータが判断を下さないまま修正を重ねると iteration が無駄に増加する。「Out of Scope」と判断した場合は次のイテレーションに持ち越さず、判断根拠を LOOP_STATE コメントに記録する。

**PR 本文形式不足のみが原因の場合の特別対処（実装コードに問題がない場合）:**

> pr-reviewer の REQUEST_CHANGES が PR 本文の形式不足のみに起因する場合（実装コードの問題がない場合）、オーケストラレータが main conversation から直接 `gh pr edit <PR番号> --body "..."` で PR 本文を修正し、Step 4 のみを再実行してよい（iteration カウントは増加させない）。
> 判断基準: pr-reviewer のブロッカーリストが「PR 本文の形式」のみを指摘していて、実装コード・AC 達成・テスト・検証に関する指摘が含まれていない場合。

## REQUEST_CHANGES / 人間フィードバック受領時の対応

人間オペレーターから REQUEST_CHANGES（修正フィードバック）を受領した際、オーケストラレータは以下のフローで対応する。**直接実装展開は禁止**する。

### 対応フロー

```
1. 人間フィードバック受領
   ↓
2. レビュー SubAgent（`review-issue`）に委譲
   └─ 設計の妥当性・影響範囲を評価させる
   ↓
3. 追加調査が必要な場合 → 調査 SubAgent に委譲
   ├─ `codebase-investigator`: リポジトリデータ調査
   ├─ `web-researcher`: 外部仕様・参考資料調査
   ↓
4. レビュー・調査結果をオーケストレーターが統合
   ├─ Issue contract の修正必要性を判定
   ├─ 実装者への追加指示を検討
   ↓
5. Issue 本文を更新（必要に応じて AC / VC / Allowed Paths を修正）
   ↓
6. 更新後の Issue を実装者 SubAgent（`implementation-worker`）に委譲
```

### 禁止パターン

オーケストラレータが人間フィードバックを受領したとき、以下の行為は**禁止**:

- ❌ フィードバック内容を直接 Issue 本文に反映し、実装者に実装させる
- ❌ レビュー SubAgent 経由なしに実装修正を指示する
- ❌ 影響範囲の検討なしに Allowed Paths を拡張する
- ❌ contract の整合性を検証しないまま Issue 本文を変更する

### 必須チェックリスト

フィードバック受領時に以下を確認してから次 iteration へ進むこと:

- [ ] レビュー SubAgent による設計評価がなされているか
- [ ] 影響範囲（Allowed Paths の変更）が特定されているか
- [ ] 追加調査が必要なら、調査 SubAgent に委譲済みか
- [ ] Issue contract（AC / VC）の修正が必要か判定されているか
- [ ] 修正後の Issue 本文が整合性チェックを通っているか

---

**ループ継続の場合:**

フィードバックをまとめて Issue にコメントし、Step 1 から再実行する（`iteration` を +1）。

**handoff 正本（handoff_artifact）の選定と記録:**

Feedback コメントを Issue に投稿した後、そのコメント URL を次反復の **handoff 正本（handoff_artifact）** として扱う。handoff 正本は次反復の Step 1 委譲プロンプトに必ず含め、SubAgent が参照すべき唯一の成果物とする。

- **handoff_artifact の選定基準**: 当該 iteration で投稿した Feedback コメント URL を1件だけ選択する（最新の Feedback コメントが正本）。
- **supersedes フィールド**: 前回 iteration の handoff_artifact が存在する場合は `supersedes: <前回の Feedback コメント URL>` で旧成果物の置換を明示する。初回（iteration 1）は `supersedes: none` とする。
- **再依頼 ledger**: 同一 SubAgent thread を再利用する場合は `agent_thread_reuse`, `previous_agent_id_or_task`, `reuse_method`, `previous_findings`, `fix_delta`, `handoff_artifact` を必須記録する。

```bash
gh issue comment <Issue番号> --body "$(cat <<'EOF'
## LOOP_STATE
```yaml
iteration: <N>
phase: feedback
status: running
pr_url: <PR_URL>
last_verdict: <APPROVE or REQUEST_CHANGES>
active_rules: <id1, id2, ...>
handoff_artifact: <この Feedback コメント URL>
supersedes: <前回の Feedback コメント URL or none>
agent_thread_reuse: <true/false>
previous_agent_id_or_task: <agent id または task 名>
reuse_method: <send_input|resume_agent + send_input|new_agent>
previous_findings:
  - <前回の主要指摘>
fix_delta:
  - <次 iteration で実施する修正>
```
## Feedback
<test-runner・adversarial-reviewer・pr-reviewer のフィードバックをまとめて記載>
EOF
)"
```

**iteration 終了時の `active_rules:` 書き込み手順**: 上記 LOOP_STATE コメントを投稿する際、`active_rules:` フィールドには Step 0 で確定した rule-id セット（preparation.md の Step 0 手順 4 で記録した `active_rules:` の値）をそのまま転記する。これにより次 iteration の冪等チェック（preparation.md Step 0 手順 3）が「既に同 rule-id が記録されている → 再 Read しない」と正しく判断でき、preflight 冪等チェックの閉ループが成立する。

次反復の Step 1 委譲プロンプトには以下を追加すること:
```
handoff_artifact: <Feedback コメント URL>
supersedes: <前回の Feedback コメント URL or none>
previous_findings:
  - <前回の主要指摘>
fix_delta:
  - <今回の修正方針>
```

---

## Loop Termination

### 終了条件（全て満たした場合にループ終了）

1. `pr-reviewer` が `LOOP_VERDICT: APPROVE` を発行している
2. `adversarial-reviewer` の**正規化後** CRITICAL 件数が 0 件
3. `adversarial-reviewer` の**正規化後** HIGH 件数が 0 件
4. PR の `mergeable == MERGEABLE` かつ `mergeStateStatus != DIRTY|BLOCKED`（merge conflict がないこと）
5. **未レビュー commit ガード**: PR の現在の head SHA が LOOP_VERDICT に記録された `reviewed_head_sha` と一致すること（一致しない場合は別ワークツリー等に由来する未レビュー commit が混入しているため、ループを終了させない／Step 2+3 を再実行する。詳細は下記「未レビュー commit 検出時の処理」参照）。

> 2 と 3 の件数判定には、生の adversarial-reviewer 件数ではなく Step 3.5 で記録した `normalized_critical_count` / `normalized_high_count` を用いる。`contradiction_findings` / `repeated_out_of_scope_findings` / `wip_scope_downgraded_findings` に入った所見は終了判定の blocker に含めない。

PR の `LOOP_VERDICT` 構造化ブロック（`## LOOP_VERDICT` セクション内の YAML コードブロック）を正として読み取る:

    ## LOOP_VERDICT
    ```yaml
    verdict: APPROVE
    blockers: []
    mergeable: MERGEABLE
    mergeStateStatus: CLEAN
    reviewed_head_sha: <Step 3 pre-fetch で固定した SHA>
    ```

`verdict: APPROVE` かつ `blockers: []`（空リスト）かつ `reviewed_head_sha` が現在の PR head SHA と一致する場合にのみ終了とみなす。

### 未レビュー commit 検出時の処理（Reviewed Head SHA Guard）

PR head SHA が `reviewed_head_sha` と不一致の場合（別ワークツリー由来の commit 混入、または iteration の最終局面で追加 push が発生した等）、オーケストラレータは以下を実施する:

1. **ループ終了をブロックする**: `LOOP_VERDICT: APPROVE` であってもループを終了させない。
2. **未レビュー commit を確認する**:
   ```bash
   gh pr view <PR番号> --json headRefOid -q .headRefOid
   git log --oneline <REVIEWED_HEAD_SHA>..<CURRENT_HEAD_SHA>
   ```
3. **LOOP_STATE に記録する**: 未レビュー commit の検出を明示し、`reviewed_head_sha` と現在の head SHA を併記する。
4. **Step 2+3 を再実行する**: 新しい head SHA を `REVIEWED_HEAD_SHA` として再記録し、test-runner / adversarial-reviewer に再度委譲する。Step 4 もその後に再実行する。
5. **iteration カウントの扱い**: 未レビュー commit ガードによる再実行は通常の修正フィードバックループと同様に iteration を +1 する（無限ループ防止のため `max_iterations: 5` の上限が適用される）。

#### reviewed_head_sha が LOOP_VERDICT に未記載の場合（pr-reviewer 記載漏れ）

LOOP_VERDICT YAML から `reviewed_head_sha` フィールドを抽出できなかった場合（pr-reviewer が必須フィールドを書き漏らしたケース）は、未レビュー commit 不一致とは異なる処理方針（iteration カウント +1 しない、既存コメント上書きしない）で扱う。実装上は同一の if-elif 分岐内で処理する:

1. **iteration カウントは +1 しない**: 同一 iteration 内の Step 4 のみを再実行する（実装コード変更がないため）。
2. **LOOP_STATE に記録する**: 「pr-reviewer が reviewed_head_sha を記載漏れ」を明示的に記録する（次回のスキル運用改善のためにも痕跡を残す）。
3. **pr-reviewer 再投稿の方針**: 既存の LOOP_VERDICT コメントは**上書きせず残し**、新しい `gh pr review --comment` で **新規** verdict コメントを投稿させること（Step 5 自動読み取りクエリは `last` で取得するため、新規投稿が新しい正本となる）。
4. **再投稿プロンプトに必ず含める**: 「前回の LOOP_VERDICT コメントには `reviewed_head_sha` が未記載であった。今回の再投稿では LOOP_VERDICT YAML に `reviewed_head_sha: <REVIEWED_HEAD_SHA>` を**必ず**含めること」を明示する。
5. **再発防止**: pr-reviewer が 2 回連続で `reviewed_head_sha` を記載漏れした場合は人間オペレーターにエスカレーションする（自動ループ継続しない）。カウンタ `REVIEWED_HEAD_SHA_OMISSION_COUNT` を LOOP_STATE の専用フィールドで保持し、2 以上に達した時点で `phase: escalation` に遷移させる（下記「自動読み取り実装例」内の bash 実装参照）。

> **重要**: このガードは PR #744 / Issue #707 で発生した「別ワークツリー由来の未レビュー commit が main にマージされた」事故の再発防止が目的である。LOOP_VERDICT の verdict / blockers / mergeable のいずれが満たされていても、`reviewed_head_sha` 不一致の場合は**必ずループを継続**すること。


### Merge Conflict 検出時の処理（Mergeable Conflict Handling）

PR merge conflict が検出された場合（`mergeable=CONFLICTING` または `mergeStateStatus=DIRTY|BLOCKED`）:

1. **conflict blocker として記録する**: pr-reviewer（Step 4）が自動的に REQUEST_CHANGES を発行する（pr-review-judge SKILL の Step 1.5 参照）。
2. **iteration の判定**:
   - conflict が実装コードの問題に起因する場合（実装ロジック・コードの衝突）: Step 1 へフィードバックして修正させる（ループ継続）。
   - conflict が main ブランチ側の変更に起因する場合（main が進んだことによる自動衝突）: 人間オペレーターにエスカレーションする。conflict の解消は manual rebase または gh CLI の `--auto-merge` 設定など、PR author による判断が必要。
3. **LOOP_STATE に記録する**: conflict 検出を明示的に記録し、次 iteration への handoff で「conflict 解消が必須」と明記する。

### Step 2 FAIL が baseline failure に帰属する場合の Halt with Improvement Notes

Step 2（test-runner）で TEST_VERDICT: FAIL が検出された場合、以下の判定フローを実施する:

1. **fail の種別を分類する**:
   ```bash
   # test-runner PR コメントから TEST_VERDICT YAML の baseline_only フィールドを抽出
   # machine-readable marker で test-runner コメントを特定し、YAML から baseline_only 値を抽出
   # この値が true の場合、失敗は baseline failure のみで今回差分 blocker がない
   BASELINE_ONLY=$(gh pr view <PR番号> --json comments --jq '
     [.comments[] | select(.body | contains("<!-- TEST_VERDICT_MACHINE v1 -->"))] | last | .body
   ' | grep -E "^[[:space:]]*baseline_only:" | head -n1 | sed -E 's/.*baseline_only:[[:space:]]*//; s/[[:space:]]*$//')
   ```

2. **判定基準**:
   - **baseline_only: true**（今回差分 Blocker がない）: PR 外の既存問題
     - → **ハルト決定**: LOOP_STATE に `phase: halted_baseline` を記録
     - → improvement notes をオーケストラレータが投稿して停止
   - **baseline_only: false / 未指定**（今回差分 Blocker がある）: PR 内での修正が必要
     - → 通常ループ継続（Step 5 判定へ進み、フィードバックして Step 1 再実行）

3. **Halt with Improvement Notes 実施手順**:
   ```bash
   gh issue comment <Issue番号> --body "## LOOP_STATE
\`\`\`yaml
iteration: <N>
phase: halted_baseline
status: blocked_without_blocker
pr_url: <PR_URL>
reason: baseline_failure_only
improvement_notes_posted: true
\`\`\`
## Halt: Test Failure は Baseline Failure のみ

本 iteration の test-runner 検証で以下の結果が得られました:

### Summary
- **今回差分 Blocker**: なし
- **Baseline Failure（既存問題）**: あり（詳細は PR コメント参照）

### 判定
PR の実装部分に由来する失敗は検出されませんでした。テスト失敗は PR 外の既存問題（baseline failure）に帰属しています。

### 改善メモ
以下の点を念頭に、次アクション（Issue 作成・既存 Issue への追跡等）を検討してください:

1. **既知の baseline failure の追跡**:
   - baseline failure の詳細は PR #<PR番号> の test-runner コメントを参照
   - 既存 Issue が追跡済みか確認し、未追跡の場合は新規 Issue を作成

2. **PR マージ可否の判定**:
   - 実装品質（test-runner PASS、adversarial-reviewer CRITICAL/HIGH 0件）が確認できたら、baseline failure に関わらず PR をマージしてよい
   - baseline failure の修正は別 Issue として分離し、PR マージを阻害しない

このループは停止されています。以後の操作（PR マージ等）は人間が手動で進めてください。"
   ```

4. **ループ終了**: この判定以降、自動ループは継続しない（人間判断待ち）。

### 正常終了時の処理

```bash
gh issue comment <Issue番号> --body "## LOOP_STATE
\`\`\`yaml
iteration: <N>
phase: completed
status: success
pr_url: <PR_URL>
last_verdict: APPROVE
\`\`\`
## Result
ループ終了: pr-reviewer APPROVE、adversarial-reviewer 正規化後 CRITICAL/HIGH 0件（除外した所見がある場合は LOOP_STATE / PR コメントを参照）。
PR をレビュー待ちに移行します。"
```

### max_iterations 超過時のエスカレーション

`max_iterations: 5` を超えた場合（iteration が 5 に達した時点でループ終了条件が満たされていない場合）は即座に停止し、人間にエスカレーションする:

```bash
gh issue comment <Issue番号> --body "## LOOP_STATE
\`\`\`yaml
iteration: 5
phase: escalation
status: blocked
pr_url: <PR_URL>
last_verdict: <last_verdict>
\`\`\`
## Escalation: max_iterations 超過

5回のイテレーションを経ても終了条件を満たしませんでした。
人間オペレーターの判断が必要です。

### 未解決の問題
<最後のフィードバックを記載>

### 必要なアクション
- [ ] 問題の根本原因を確認し、Issue contract の修正または手動対応を判断してください。"
```
