---
name: open-pr
description: 承認済みの implementation issue の PR を起票するときに使う。publish ゲート（人間承認）/ Closes/Refs 自動判定 / idempotency チェック（同一ブランチの既存 PR detect）/ `gh pr create` 実行を担当する独立スキル。implement-issue / impl-review-loop から委譲され、PR 起票責務を一箇所に集約する。
---

# Open PR

承認済み issue の PR を起票する専用スキル。`implement-issue` / `impl-review-loop` から委譲して PR 作成ロジックを一箇所に集約する。

## Input（入力）

呼び出し元（`implement-issue` 等）から以下を受け取る。

**必須:**
- `pr_title`: 例 `feat(systems): MovementSystem に境界クランプを追加`
- `linked_issue`: linked issue 番号（PR 本文の `Closes` / `Refs` に使う）
- `publish`: `yes` が明示されていない場合は PR 作成を中断する（人間承認ゲート）
- `pr_body`: PR 本文の Markdown

**任意:**
- `dry_run`: `true` で PR 作成プレビューのみ実行（gh pr create はしない）
- `draft`: `true` で Draft PR として作成（デフォルト: true）
- `branch`: ブランチ名（省略時は現在の HEAD ブランチを使う）
- `overlap_preflight`: `check_implementation_overlap.py` の overlap preflight evidence を PR 作成直前に強制検証させるための入力（Issue #1458）。フィールド:
  - `required`: `true` / `false`。ただし linked issue が `phase/implementation` ラベルを持つ場合、`open_pr.py` が自らラベルを判定して `false` でも gate を省略しない（bypass-via-omission 対策、AC2）
  - `evidence_file`: `check_implementation_overlap.py` が出力した evidence JSON（`IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1`）のパス
  - `expected_evidence_sha256`: `sha256:...`。stored evidence file の embedded `evidence_sha256`（`collected_at` / `decision_inputs_sha256` を含めて計算された、timestamp 込みの artifact 全体ハッシュ）との一致確認に使う
  - `expected_decision_inputs_sha256`: `sha256:...`。`collected_at` 等の timestamp フィールドを含めずに計算された `decision_inputs_sha256`（timestamp 非依存）と、`open_pr.py` が `gh pr create` 直前にオンライン再実行して得た fresh `decision_inputs_sha256` との drift 比較に使う

  両ハッシュの canonicalization 契約は `check_implementation_overlap.py` の producer 実装と同一（`json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))` を経て sha256 hex 化し `sha256:` を前置する。evidence ファイルは `indent=2` で pretty 保存されるため素の `sha256sum evidence_file.json` とは一致しない）。

  CLI 引数（`scripts/open_pr.py`）:
  ```bash
  uv run --locked python3 .claude/skills/open-pr/scripts/open_pr.py \
    --pr-title "<title>" \
    --linked-issue <N> \
    --publish yes \
    --pr-body-file /tmp/pr-body.md \
    --overlap-preflight-required \
    --overlap-preflight-evidence-file /tmp/overlap_preflight_<N>_recheck.json \
    --overlap-preflight-expected-evidence-sha256 "sha256:<evidence_sha256>" \
    --overlap-preflight-expected-decision-inputs-sha256 "sha256:<decision_inputs_sha256>"
  ```

## Procedure（手順）

### 1. Publish ゲート

`publish: yes` が明示されていない場合、`E_APPROVAL_MISSING` を返して停止:

```
[ERROR] E_APPROVAL_MISSING: publish: yes が指定されていません。
人間承認後、publish: yes を明示して再度呼び出してください。
```

### 2. final PR body の validator 実行

`open_pr.py` は linked issue state を解決して `Closes` / `Refs` を final body に反映した後、`validate_pr_body.py` を実行する。

validator CLI（検証 CLI）:
```bash
uv run --locked python3 .claude/skills/open-pr/scripts/validate_pr_body.py \
  --body-file <final-pr-body-file> \
  --changed-paths-file <changed-paths-file> \
  --linked-issue <N>
```

JSON schema は `loop_body_lint/v1` (`target: "pr"`)、exit code は pass=0 / fail=1 / internal=2 とし、判定結果を機械的に確認する。

`validate_pr_body.py` が担う rule:
- LP050: Schema Consumer Inventory 必須条件
- LP051: safety-sensitive PR に対する Safety Claim Matrix 必須条件
- LP052: 必須セクション欠落
- LP053: Schema Change Applicability decision 不正
- LP055: Safety Claim Matrix 列欠落
- LP056: `Not controlled` 非空時の Follow-up 欠落
- LP057: final PR body の related issue 欠落
- LP058: changed paths 未解決

validator が `fail` または `internal` を返した場合、`open_pr.py` は **`gh pr create` を呼ばず fail-closed** で停止する。

### 3. Linked Issue 状態確認 + Closes / Refs 自動判定

```bash
ISSUE_STATE=$(gh issue view <linked_issue> --json state --jq '.state')
```

- `OPEN` → `Closes #<linked_issue>` を PR 本文に追記
- `CLOSED` → `Refs #<linked_issue>` に downgrade（自動マージで誤って再 close しないため）し、WARN を出力
- 状態取得失敗 → `E_LINKED_ISSUE_STATE_UNKNOWN` を返して停止

PR 本文に既に `Closes #N` / `Refs #N` がある場合は、上記判定と一致するかを確認し、不一致なら本文側を優先（caller の意図を尊重）。

### 3.5. Parent Child Materialization（delivery-rollup parent の child PR の場合の親子 materialization）

linked issue の parent が `parent_mode: delivery-rollup` の場合、PR 本文に `## Parent Child Materialization` セクションを追加する。LLM と人間レビュアーが parent の残り child 状態を PR 本文から把握できるようにする。

```bash
# parent issue 番号を linked issue から取得
PARENT_NUM=$(gh api repos/{owner}/{repo}/issues/<linked_issue>/parent --jq '.number // empty')

# parent が delivery-rollup かどうか確認
if [ -n "$PARENT_NUM" ]; then
  PARENT_MODE=$(gh issue view "$PARENT_NUM" --json body --jq '.body' \
    | grep -oP 'parent_mode:\s*\K[\w-]+' | head -1)
fi
```

`parent_mode: delivery-rollup` の場合のみ `plan_child_materialization.py` を実行して PR 本文に含める:

```bash
uv run --locked python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
  --repo <owner>/<repo> \
  --issue "$PARENT_NUM"
```

PR 本文に追加する `## Parent Child Materialization` セクションのテンプレート:

```markdown
## Parent Child Materialization

- parent_issue: #<parent_num>
- parent_mode: delivery-rollup
- child_materialization_plan: pass | pending | n/a
- unresolved_children: <C254-3, C254-4, ... | なし>

<CHILD_MATERIALIZATION_PLAN_V2 の summary（missing / stale_body_only エントリのみ列挙）>
```

- `child_materialization_plan: pass` — 全 child が `existing` または `no_op`
- `child_materialization_plan: pending` — `missing` / `stale_body_only` / `human_escalation` が残っている
- `child_materialization_plan: n/a` — linked issue が delivery-rollup parent の child でない

parent が存在しない、または `parent_mode` が `delivery-rollup` でない場合は本セクションを省略する（n/a 扱い）。

### 3.5. changed paths の決定論的解決

`--changed-paths` が未指定の場合、`open_pr.py` は `git merge-base main HEAD` と `git diff --name-only <merge-base>...HEAD` で changed paths を解決する。

- 解決成功 → validator に file 経由で渡す
- 解決失敗 → validator が `LP058` を返し、PR 作成を停止する

### 4. Idempotency チェック（既存 PR 検出）

```bash
EXISTING_PR=$(gh pr list --head <branch> --state open --json number,url --jq '.[0]')
```

- 既存 PR あり → 重複作成せず、既存 PR URL を返す（必要なら本文 update を提案）
- 既存 PR なし → 次のステップへ

### 4.5. Overlap Preflight Hard Gate（`gh pr create` 直前のオンライン再検証による drift（乖離）/ unsafe route（危険経路）検出、Issue #1458 / repository binding は Issue #1470）

`open_pr.py` は既存 PR 検出・dry-run 処理より後、`gh pr create` 呼び出し直前に、`overlap_preflight` が `required: true` の場合、または linked issue が `phase/implementation` ラベルを持つ場合（呼び出し元が `overlap_preflight` を未指定または `required: false` としていても、AC2 の bypass-via-omission 対策により省略されない）、以下を実行する:

0. PR mutation target の `canonical_repository` 解決（Issue #1470）: `--repo` または `git remote` から得た requested repository を `resolve_canonical_repository()` で GitHub Repository API（`GET /repos/{owner}/{name}`）を通じて **一度だけ** 解決し、`full_name` の小文字化形を `canonical_repository` とする。以降のオンライン再実行（`--repo`）と `gh pr create --repo` の両方に、この単一の `canonical_repository` 変数を使う。API 呼び出しに失敗した場合は静的正規化へフォールバックせず `E_OVERLAP_PREFLIGHT_SOURCE_FAILURE` で停止する（`check_implementation_overlap.py` の producer 側 `_canonicalize_repo(online=True)` はオフライン fallback を持つが、consumer 側の `resolve_canonical_repository()` は持たない）。mixed-case な入力（例 `SQUNE121/LOOP-PROTOCOL`）や rename / transfer 後の alias も、この単一の API 呼び出しで現在の `full_name` へ解決される。
1. `evidence_file` の再読込と `expected_evidence_sha256`（stored evidence の embedded `evidence_sha256` との一致確認、evidence 自体の integrity 検証）。stored evidence は `repository`（string, 非空, canonical 小文字化形）を required field として持たなければならない。欠落・型不正・非canonical・`canonical_repository` との不一致は、いずれもオンライン再実行（3）より **前** に `E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID` で停止する。`repository` field 自体を持たない legacy V1 evidence は、正しい legacy hash を持っていても再生成要求として拒否される（新しい evidence を `check_implementation_overlap.py` で再生成すること）
2. stored evidence の `decision_inputs_sha256` と、呼び出し元が指定した `expected_decision_inputs_sha256` との一致確認（PR #1467 review fix, P2-1: stored artifact がどの preflight collection chain に属するかを確定する provenance チェック。ここで不一致なら `E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID` で停止し、オンライン再実行は行わない）
3. integrity 確認済み stored evidence の `source.limit` を正の整数として検証し、その値以外の caller input は使わず、`check_implementation_overlap.py`（`.claude/skills/implement-issue/scripts/check_implementation_overlap.py`。producer は変更せず subprocess として再実行するのみ）へ同一 `--repo <canonical_repository>` / `--issue-number` / `--limit <stored source.limit>` でオンライン再実行する。stored evidence の `source` に collection contract（`collection_mode` / `page_size` / `page_count` / `fetched_count` / `has_next_page`、#1493 AC1）のいずれかが欠けている場合は、GraphQL cursor pagination 未対応の legacy evidence として `E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID` で再収集を要求する（オンライン再実行より前に停止する）
4. fresh evidence の `repository` field（string, `canonical_repository` と一致必須）を、汎用の `decision_inputs_sha256` drift 検出（5）より **前** に検証する（Issue #1470, AC5）。この順序は、`repository` binding という明示的不変条件（PR mutation target の identity）を、汎用ハッシュ比較より先に独立した predicate として確認し、検証順序とエラー分類（`E_OVERLAP_PREFLIGHT_DRIFT` の原因が repository binding 由来か、その他の decision input drift 由来かを区別可能にする）を安定させるためのものであり、`decision_inputs_sha256` の偶然の一致防止を主目的とするものではない。欠落・不一致は `E_OVERLAP_PREFLIGHT_DRIFT` で停止し、`gh pr create` は呼ばれない
5. fresh evidence の `source.limit` が正の整数かつ stored `source.limit` と一致すること、さらに fresh evidence の `decision_inputs_sha256` と `expected_decision_inputs_sha256` が一致することを確認する（collection 時点からの drift 検出。上記 2 で stored と expected の同一性が既に確認されているため、fresh がここで一致すれば stored・fresh 双方が同一 collection chain に属することが保証される）。fresh evidence の collection contract（`collection_mode` / `page_size` / `page_count` / `fetched_count` / `has_next_page`）が欠けている、または `collection_mode` が stored と一致しない場合も `E_OVERLAP_PREFLIGHT_DRIFT` で拒否する（#1493 AC3。caller は collection contract を上書きできない — 唯一の入力は integrity 確認済み stored evidence の `source.limit` のみ）
6. fresh evidence の `route`（`proceed` / `proceed_with_collision_evidence` のみ安全）・`source.complete`（`true` 必須）・`source.saturated`（`false` 必須）・`validation_errors`（空必須）・`dependency_resolution.unresolved_refs`（空配列必須）・`dependency_resolution.blocking_predecessor`（`null` 必須）・`current_issue.number`（`linked_issue` と一致必須）の安全性 predicate 検証

repository A の stored evidence を、同じ Issue 番号を持つ repository B の PR 作成に再利用しようとした場合、上記 1 の stored repository 不一致チェックによってオンライン再実行（3）より前に必ず拒否され、`gh pr create` は一度も呼ばれない（Issue #1470, AC7）。

Issue #1477 の限定例として、fresh evidence の `human_review_required` が #519・#520・#1429 の `readback_incomplete` **だけ**に起因するときは、次の全条件を満たす `overlap_readback_waiver` を live Issue body と同一 SHA の `CONTRACT_REVIEW_RESULT_V1 status: go` snapshot から検証してから、その3件だけを safe route 判定から除外できる。

- `issue_numbers: [519, 520, 1429]`
- `reason: human_approved_readback_ignore`
- `expires_on: "2026-07-13"`（当日を含む）
- `approved_by: user_session`

対象外 Issue、他の incomplete candidate、`readback_incomplete` 以外の reason、期限切れ、live body / snapshot SHA 不一致、または waiver のキー・値不一致はすべて fail-closed とする。この例外は `source`、`validation_errors`、依存解決、current issue binding の既存 predicate を緩めず、任意 waiver を受け付ける一般機構ではない。

いずれかが不成立の場合、`gh pr create` を呼ばず fail-closed で停止する（下記 Error Codes 参照）。オンライン再実行に使う `--repo` は `gh pr create --repo` にもそのまま渡される同一の `canonical_repository` 変数であり、これが AC8（Issue #1458）の cross-repo binding mitigation の根拠。evidence 自体への `repository` フィールド追加は #1462（マージ済み）、consumer 側の required-field 検証・canonical identity 束縛・legacy evidence 拒否は #1470（本節）の scope。

`overlap_gate_active`（gate 起動要否, `forced_by_label` 判定）は `gh pr create` 呼び出し直前に毎回オンラインで linked issue の labels を再取得して決定する（PR #1467 review fix, P1-1）。処理前半で取得した labels のキャッシュはこの security decision には使わない（TOCTOU 対策）。labels 再取得が失敗した場合（認証エラー・JSON 不正・型不正等）は「ラベルなし」として扱わず fail-closed（gate を必ず有効化する）。

注記: 本機構は「TOCTOU を完全に排除する」ものではない。GitHub の PR 作成 API には issue body の SHA に紐づく precondition / If-Match 機構が存在しないため、これは **mutation 直前の bounded freshness gate**（race window を狭める設計）であり、atomic な保証ではない。

`dry_run: true` の場合、本 gate は実行されない（`gh pr create` 自体を呼ばないため）。

#### 既知の限界（暫定的 mitigation であることの明記, PR #1467 review fix, #1470 review fix で native dependency 記述を修正）

- producer（`check_implementation_overlap.py`）は native GitHub issue dependency の `blocked_by` / `blocking` を GitHub 公式 REST issue-dependencies endpoint（`repos/{repo}/issues/{issue_number}/dependencies/{direction}`）から全ページ取得し、Machine-Readable Contract の `blocked_by:` YAML および legacy `Depends on #N` テキスト参照と統合済みである（producer は `implement-issue` skill、Allowed Paths 外であり本 skill の Allowed Paths では変更できない）。
- 候補 Issue 収集の全件性は GraphQL cursor pagination の `pageInfo.hasNextPage` によって証明され、stored/fresh 双方の collection contract 一致を本 gate が検証する（#1493）。残存限界は、repository ID（`id` / `node_id`）による binding 未実装（`owner/name` の canonical full_name 小文字化形までの binding）、および PR mutation との非原子性（本節末尾の bounded freshness gate の注記を参照）である。
- `repository` フィールドの producer 側 schema migration（#1462、additive migration・V1 のまま拡張、マージ済み）と、consumer 側の required-field 検証・canonical identity 束縛（#1470、本節）により、stored/fresh evidence と `gh pr create --repo` の cross-repo binding gap（#1458 が残した gap）は解消されている。
- `repository` field の binding は `owner/name` の canonical full_name（小文字化形）までであり、repository ID（`id` / `node_id`）による binding は行わない（将来の evidence schema V2 で検討、#1470 の Out of Scope）。

### 5. PR 作成

dry_run 時はここでプレビューを表示して終了（gh pr create は実行しない）。

```bash
PR_URL=$(gh pr create \
  --title "<pr_title>" \
  --body-file "<validated-final-body-file>" \
  $([ "$draft" = "true" ] && echo "--draft") \
  --head "<branch>" \
  --base main)
```

### 6. Output（KEY=VALUE stdout contract の標準出力契約）

```
PR_URL=https://github.com/<owner>/<repo>/pull/<number>
PR_NUMBER=<number>
LINKED_ISSUE=<linked_issue>
LINK_KIND=Closes | Refs
EXISTING=true | false
DRY_RUN=true | false
```

dry_run 時:
```
DRY_RUN=true
PR_URL=
PR_TITLE_PREVIEW=...
PR_BODY_PREVIEW_FIRST_LINES=...
```

エラー時:
```
ERROR=E_APPROVAL_MISSING | E_PR_BODY_VALIDATION_FAILED | E_LINKED_ISSUE_STATE_UNKNOWN | E_GH_FAILURE | E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING | E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID | E_OVERLAP_PREFLIGHT_DRIFT | E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE | E_OVERLAP_PREFLIGHT_SOURCE_FAILURE
ERROR_DETAIL=<エラー詳細>
```

## Implementation: Python wrapper（Python ラッパー実装）

本手順は `scripts/open_pr.py` に集約されている。skill 呼び出し側は以下のように起動:

```bash
uv run --locked python3 .claude/skills/open-pr/scripts/open_pr.py \
  --pr-title "<title>" \
  --linked-issue <N> \
  --publish yes \
  --pr-body-file /tmp/pr-body.md \
  --draft true
```

dry_run:
```bash
uv run --locked python3 .claude/skills/open-pr/scripts/open_pr.py \
  --pr-title "<title>" \
  --linked-issue <N> \
  --publish yes \
  --pr-body-file /tmp/pr-body.md \
  --dry-run
```

## Error Codes（エラーコード）

| code | 意味 | 復旧手順 |
|---|---|---|
| `E_APPROVAL_MISSING` | `publish: yes` 未指定 | 人間承認を得て `publish: yes` で再実行 |
| `E_PR_BODY_VALIDATION_FAILED` | `validate_pr_body.py` が fail / internal を返した（Schema Consumer Inventory 欠落以外の一般的な validation 失敗） | `VALIDATOR_RULE_IDS` と `ERROR_DETAIL` を確認し、該当 section / changed paths / validator 出力を修正して再実行 |
| `E_SCHEMA_CONSUMER_INVENTORY_MISSING` | `schema_change` / `uncertain` PR で Schema Consumer Inventory が欠落または placeholder（LP050 / LP052 による検出） | `## Schema Consumer Inventory` セクションを追加し、before/after、consumer 列挙、更新状況を記載する |
| `E_LINKED_ISSUE_STATE_UNKNOWN` | linked issue の state 取得失敗 | gh 認証 / linked_issue 番号を確認 |
| `E_GH_FAILURE` | `gh pr create` 失敗 | stderr の詳細を確認、リポジトリ権限 / ブランチ存在 / リモート push 済みを確認 |
| `E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING` | `overlap_preflight` gate が有効（`required: true` または `phase/implementation` ラベルによる強制）だが `evidence_file` が存在しない・読み込めない | `check_implementation_overlap.py` を再実行して evidence file を再生成し、正しいパスを渡す |
| `E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID` | stored evidence の parse 失敗・スキーマ不一致・embedded `evidence_sha256` と `expected_evidence_sha256` の不一致、stored `source.limit` の欠落・非整数・0以下、stored `repository` の欠落・型不正・非canonical・`canonical_repository` との不一致（Issue #1470）、または stored `source` の collection contract フィールド（`collection_mode` 等、#1493）の欠落 | evidence file が破損していないか、`expected_evidence_sha256` と stored `source.limit` / `repository` / collection contract が正しいか確認し、必要なら再収集する |
| `E_OVERLAP_PREFLIGHT_DRIFT` | オンライン再実行の fresh `decision_inputs_sha256` が `expected_decision_inputs_sha256` と不一致、fresh `source.limit` の欠落・非整数・0以下・stored 値との不一致、fresh `repository` の欠落・`canonical_repository` との不一致（Issue #1470）、または fresh `source` の collection contract フィールドの欠落・`collection_mode` の stored との不一致（#1493） | `implement-issue/SKILL.md` の overlap preflight（Step 2）を再実行し、新しい evidence で再度 `open_pr.py` を呼び出す |
| `E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE` | fresh evidence の `route` / `source.complete` / `source.saturated` / `validation_errors` / `dependency_resolution.unresolved_refs` / `dependency_resolution.blocking_predecessor` / `current_issue.number` のいずれかが不安全 | `route` と関連フィールドを確認し、`wait_for_predecessor` / `human_review_required` / `duplicate` 等の場合は人間判断へ停止する |
| `E_OVERLAP_PREFLIGHT_SOURCE_FAILURE` | `check_implementation_overlap.py` のオンライン再実行が subprocess timeout / 非ゼロ終了 / 非 JSON 出力 / 認証失敗のいずれかを起こした、または `resolve_canonical_repository()` が `canonical_repository` を解決できなかった（Issue #1470） | gh 認証・ネットワーク・スクリプトの実行環境を確認し、再実行する |

### Branch publish failure の扱い

`E_GH_FAILURE` が branch 未公開または remote head drift に起因する場合、存在しない step file を参照せず、本 `SKILL.md` と
`scripts/open_pr.py` の wrapper 結果を正本にする。復旧前に `impl-review-loop` の Publish Failure Safety Lane を参照し、
`expected_remote_head`、`current_remote_head`、`local_head`、`verified_head`、`declared_publish_head`、
`allowed_paths_gate_status`、`remote_readback_source`、`decision_inputs_complete` を比較する。
open-pr は branch publish を自前で復旧せず、比較が崩れた場合は `PUBLISH_SAFETY_STOP_REPORT_V1` を残して
impl-review-loop の publish failure lane に戻す。force update / reset へ進まない。

## Guardrails（ガードレール）

- `publish: yes` 未指定で PR を作成しない（人間承認 fail-closed）
- `validate_pr_body.py` が fail / internal を返した場合は PR を作成しない
- changed paths を解決できない場合は `LP058` により fail-closed する
- linked issue が CLOSED の場合は `Closes` を `Refs` に必ず downgrade（リンク済み close 連鎖防止）
- 同一ブランチに OPEN PR がある場合は重複作成せず既存 URL を返す
- `dry_run: true` でも publish ゲートと validator は実行する
- 既存 PR が見つかった場合、本文 update は必ず update_pr.py wrapper 経由で行う（validator bypass 防止）
- linked issue が `phase/implementation` ラベルを持つ場合、`overlap_preflight` の未指定 / `required: false` だけでは overlap preflight hard gate を省略できない（Issue #1458, AC2）

## PR 作成・PR 更新前の必須ローカル preflight

`open_pr.py` および `update_pr.py` は `gh pr create` / `gh pr edit` の直前に以下の 2 段 preflight を実行する（fail-closed）。

### preflight ステップ 1: PR body 構造バリデーション

`validate_pr_body.py` で LP050〜LP058 を検査する。

```bash
uv run --locked python3 .claude/skills/open-pr/scripts/validate_pr_body.py \
  --body-file <final-pr-body-file> \
  --changed-paths-file <changed-paths-file> \
  --linked-issue <N>
```

fail / internal の場合、mutation は実行されず `ERROR=E_PR_BODY_VALIDATION_FAILED` 等が出力される。

### preflight ステップ 2: 日本語比率チェック（`validate_japanese_content.py --threshold 0.1`）

`validate_japanese_content.py` で PR body から抽出した**各 prose block** の日本語文字比率が threshold（0.1）以上であることを検査する。`aggregate_ratio` は診断値であり、pass 条件ではない。いずれか 1 ブロックでも比率が threshold を下回ると fail となる。

```bash
uv run --locked python3 .claude/skills/create-issue/scripts/validate_japanese_content.py \
  --file <final-pr-body-file> \
  --threshold 0.1 \
  --verbose
```

日本語チェック失敗時、`gh pr create` / `gh pr edit` は実行されず以下が出力される:

```
PR_BODY_PREFLIGHT_RESULT_V1={"schema": "PR_BODY_PREFLIGHT_RESULT_V1", "status": "fail", "body_sha256": "sha256:...", "failed_blocks": N, "aggregate_ratio": 0.0XX, "threshold": 0.1}
ERROR=E_PR_BODY_JAPANESE_VALIDATION_FAILED
ERROR_DETAIL=<stderr from validate_japanese_content.py>
```

#### CI との SSOT 対応（AC7）

ローカル preflight（`validate_japanese_content.py --threshold 0.1`）と CI ジョブは同一スクリプトを参照する。

| 場所 | スクリプト | 閾値 | workflow/job |
|---|---|---|---|
| ローカル preflight（`open_pr.py` / `update_pr.py`） | `validate_japanese_content.py` | `0.1` | ローカル（`check-japanese.yml` 相当） |
| CI | `validate_japanese_content.py` | `0.1` | `check-japanese.yml` |

表記ゆれ解消: CI の workflow 名は `check-japanese.yml`（`validate-japanese.yml` ではない）。SSOT は `.github/workflows/check-japanese.yml` を参照すること。

## PR Body Japanese Check 失敗時の修復手順（CI 失敗後）

PR Body Japanese Check（`check-japanese.yml`）が失敗した場合は、`pr_body_japanese_repair_plan.py` を使って修復プランを生成し、`update_pr.py` 経由で適用する。

### ステップ 1: 修復プランの生成

```bash
# --body-file モード（PR body ファイルを直接指定）
uv run --locked python3 .claude/skills/open-pr/scripts/pr_body_japanese_repair_plan.py \
  --body-file <path-to-pr-body.md> \
  --threshold 0.1
```

または PR 番号から直接取得:

```bash
uv run --locked python3 .claude/skills/open-pr/scripts/pr_body_japanese_repair_plan.py \
  --pr <PR_NUMBER> \
  --repo <owner>/<repo> \
  --threshold 0.1
```

stdout は `PR_BODY_JAPANESE_REPAIR_PLAN_V1` の compact JSON:

```json
{
  "schema": "PR_BODY_JAPANESE_REPAIR_PLAN_V1",
  "status": "pass | repairable | human_review_required | invalid_body | gh_error",
  "threshold": 0.1,
  "failed_blocks": [...],
  "safe_rewrite_plan": [...],
  "body_file_out": null,
  "preserved_tokens": ["Closes #N", "Refs #N", ...],
  "next_action": "none | apply_safe_rewrite_plan | human_review_required"
}
```

exit code（終了コード）: `0 pass / 10 repairable / 20 human_review_required / 30 invalid_body / 40 gh_error`

日本語判定の SSOT:
- `validate_japanese_content.py` の `validate_text()` / `split_markdown_blocks()`
- `prose_boundary_policy.py` の `iter_markdown_blocks()` / `lookup_heading_policy()`

### ステップ 2: status に応じた対処

| status | 対処 |
|---|---|
| `pass` | Japanese Check は通過済み。再チェック不要 |
| `repairable` | `safe_rewrite_plan` の `action: append_japanese_note` に従い、各ブロックに日本語注記を追記し、`update_pr.py` 経由で更新する |
| `human_review_required` | 任意英語の意味変換が必要。人間が日本語翻訳を行い `update_pr.py` 経由で更新する |
| `invalid_body` | PR body が空または読み込み不能。body を確認する |
| `gh_error` | gh CLI エラー。認証 / PR 番号 / ネットワークを確認する |

### ステップ 3: 修正後の PR body を update_pr.py 経由で適用

修正した body ファイルを `update_pr.py` 経由で更新する（AC2 準拠: validator bypass 防止）:

```bash
uv run --locked python3 .claude/skills/open-pr/scripts/update_pr.py \
  --pr-number <N> \
  --body-file <path-to-repaired-body.md> \
  --linked-issue <linked-issue-num>
```

`update_pr.py` は内部で `validate_japanese_content.py` と `validate_pr_body.py` の両方を実行して
整合性を確認してから `gh pr edit` を呼ぶ。

### 保護トークン（preserved_tokens）

以下のトークンは修復プラン生成時に `preserved_tokens` に記録され、書き換えから保護される:
- GitHub closing keyword 全 variant: `close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved` + colon variant
- cross-repo reference: `owner/repo#N`
- 複数 issue 列挙: `Closes #1, #2, #3`
- `Refs #N` / `Refs owner/repo#N`
- HTML comment: `<!-- ... -->`

## PR Body Update（既存 PR への本文反映）

PR の本文を更新する場合（既存 PR 発見時など）は、必ず以下の wrapper 経由で行う（validator pre-write hook を強制）:

```bash
uv run --locked python3 .claude/skills/open-pr/scripts/update_pr.py \
  --pr-number <N> \
  --body-file <path-to-new-body.md> \
  --linked-issue <linked-issue-num> \
  --changed-paths-file <changed-paths-file>
```

direct `gh pr edit --body-file` は使用禁止（validator が bypass されるため）。

update_pr.py は以下を実行:
1. 新しい body を読み込む
2. validator pre-write hook 実行（fail-closed）
3. validator pass 後、本検証済み body を temp file に書き出す
4. gh pr edit に temp file の path を渡す（TOCTOU 安全）
5. temp file を削除

KEY=VALUE stdout contract（標準出力契約）:
```
PR_NUMBER=<N>
REPO=<owner>/<repo>
UPDATED=true
```

エラー時:
```
ERROR=E_VALIDATION_FAILED | E_UPDATE_FAILURE | E_FILE_NOT_FOUND
ERROR_DETAIL=<詳細>
VALIDATOR_RULE_IDS=<rule_ids>  # validator fail 時
```

## Related（関連）

- `.claude/skills/implement-issue/SKILL.md` — 本 skill の主な呼び出し元
- `.claude/skills/impl-review-loop/SKILL.md` — オーケストレーター（差し戻し時の再呼び出し含む）
- `.github/pull_request_template.md` — テンプレート正本（あれば）
- `docs/dev/schema-governance.md` — schema 定義・Initial Known Schemas・Consumer Inventory 義務の SSOT
- `scripts/open_pr.py` — PR 作成手順を実装する Python wrapper
- `scripts/update_pr.py` — PR body 更新 wrapper with validator pre-write hook
- `docs/dev/agent-run-report.md` — PR open 後のレポート posting handoff 規約
- `.claude/skills/implement-issue/scripts/check_implementation_overlap.py` — overlap preflight gate（Issue #1458）がオンライン subprocess 再実行する producer script（open-pr の Allowed Paths 外、変更しない）

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
