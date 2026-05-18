---
name: create-issue
description: ユーザーの要求を Terminal AI Agent が再現可能に作業できる GitHub Issue に整形するときに使う。要求分析・Scope 判定・Issue 本文生成・即時起票を行う。blocking stop（Scope 分割採否・Scope Overlap 3 択）以外は人間承認なしで `.claude/skills/create-issue/scripts/create_issue_txn.py` を実行する。「Issue 起票」「Issue 作って」「create issue」などの短文トリガーで使う。
---

# Create Issue

ユーザーの要求を分析し、Terminal AI Agent が安全・再現可能に着手できる GitHub Issue を生成するスキル。

## Use When

- ユーザーが要求を GitHub Issue として起票したいとき
- 「Issue 起票して」「Issue 作って」「Issue にまとめて」「create issue」などの短文トリガー
- 要求を `1 Issue = 1 PR` で完結する Scope に整理したいとき
- `research` / `implementation` のどちらかを見分けてテンプレートを選びたいとき

## Procedure

### 0. テンプレートを読み込む（Issue Template Guard）

- Issue 種別（`parent` / `research` / `implementation`）を判定する
- タイトル先頭に `調査:` / `実装:` / `導入:` のどれが必要かを先に確認し、種別を誤らない
- 対応するテンプレートファイル `.github/ISSUE_TEMPLATE/{種別}.yml` を読み、Issue Forms の各 `textarea` の `label` を必須セクション一覧として取得する（テンプレ更新時に自動追従）
- 以降の本文生成はこの必須セクション一覧を基準にする

### 1. 要求を分析する

- ユーザーの要求から Outcome（達成したい状態）を抽出する
- **anchor 主張を含む Issue**: Issue 本文で「既存ファイルの行番号・セクション見出し・関数名」を anchor として主張する場合は、起票前に `issue-body-authoring` の Anchor Verification Preflight を参照し、`git grep` / `rg` で hit 件数を確認してから起票する
- **follow-up Issue の場合（post-merge-cleanup / issue-refinement-loop から委譲）**: `issue-body-authoring` の「ワークフロー不具合検出時の修正方針起案ガイダンス」セクションを参照し、決定論的修正と workaround を明示比較してから Outcome を起案する
- 実装案だけでなく、運用で解決できる案も比較し、採用方針を明示する
- 要件が曖昧な場合は Issue を確定させず、`## Notes for Reviewer` に記載するか blocking stop として扱う（推測で埋めない）
- **タイトル prefix と AC の性質のセルフチェック**: `research` / `調査` を名乗る Issue に `src/` や `tests/` の実装変更が AC として入っていないか確認する。入っている場合は `implementation` / `実装` に切り替えるか、Scope を分割して別 Issue にする
- 不確実性が残る場合は `phase/research` / `state/needs-human` ラベルの付与要否を先に決める。implementation に昇格できる場合は `実装:` prefix + `phase/implementation` + `state/queued` の canonical ready tuple を正本として付与する

#### desired destination handoff guard

orchestrator（`issue-refinement-loop` / `post-merge-cleanup` 等）から follow-up 候補を受ける場合:

1. 各候補の `desired_destination` と `validated_scope_delta` を必須とする。どちらか欠落していれば `failure_reason: destination mapping required` で委譲元へ返す
2. 必須フィールドが揃っている場合は、Issue 本文の `## Background` または `## In Scope` に反映する
3. `desired_destination` をそのまま write-capable claim や scope 拡張に昇格させず、repo reality で validated な範囲だけを `## In Scope` に入れる

#### sweep / cleanup issue 起票前ガード

タイトル / Outcome / VC のいずれかに `sweep` / `cleanup` / `残存` / `repo 全体` のような repo-wide inventory シグナルがある場合は、起票前に **dry-run** を必須化する:

1. Issue 本文へ入れる予定の VC または同値の read-only コマンドを実行し、`remaining_count` と `sample_hits`（最大 3 件）を取得する
2. `remaining_count == 0` の場合は **新規 issue を起票しない**。「対象 0 件のため起票不要」と報告して終了
3. dry-run でヒットがあっても、同じ concern を扱う既存 open issue が見つかった場合は新規起票せず canonical destination（Issue 番号と comment URL）を返す
4. 起票中止時の報告は `decision` / `remaining_count` / `sample_hits` / `inspected_commands` / `canonical_destination` を含める

### 1.5. 類似 Issue のキーワードベース重複チェック（起票前検索）

- ステップ 1 で抽出した Outcome・タイトル案から代表キーワード（2〜3 語）を選ぶ
- 以下のコマンドで OPEN Issue を検索する:
  ```bash
  gh issue list --search "<keyword>" --state open --json number,title,url
  ```
- 結果の解釈:
  - **重複あり（同一・極めて近い Outcome の OPEN Issue がある）**: 即座に新規起票を中止し、人間に提示:
    ```
    [類似 Issue 検出] 以下の OPEN Issue が同一・類似の Outcome を持つ可能性があります:
    - #<number>: <title> (<url>)
    1. 既存 Issue (#<number>) に追記して対応する
    2. 既存 Issue を重複クローズ候補として指定し、新規 Issue に統合する
       → 新規 Issue に「Closes #<number>（重複）」を記載して起票
    3. scope が異なることを確認し、新規 Issue を起票する（理由を明記）
    ```
  - **canonical destination が確定している場合**: 3 択提示せず、`decision: route_to_existing_issue` と `canonical_destination` を返して停止
  - **false positive の除外**: GitHub Full-Text Search はトークン分割するため hit に false positive が含まれうる。タイトル・本文を目視確認して無関係のものは除外してから判定

### 2. Scope を判定する

- `1 Issue = 1 PR` で完結する Scope かを確認する
- AC は検証可能な記述にし、実装か調査かを自分で確認する。研究寄りなら `調査:`、実装寄りなら `実装:` をタイトル先頭に置く
- Scope が複数に分かれる場合は、分割案と各 Issue の Outcome を提示して人間に確認する

### 2.5. `proposal_only` で Issue 本文案を受ける場合の境界

- Gemini CLI に下書きだけを委譲したい場合は、`gemini-cli-headless-delegation` の wrapper へ `tool_profile: proposal_only` と `output_sections: ["issue_authoring_draft"]` を明示する
- 返却された `issue_authoring_draft` は **proposal text** として扱い、そのまま GitHub に投稿済みの本文や確定済み outcome とみなさない
- final file edit / shell edit / GitHub mutation は Claude 側 worker または main thread が保持する
- request に `post_to_issue_url`、direct file edit、shell execution、GitHub mutation 指示が混ざる場合は fail-closed とし、caller 側で request を修正してから再実行する

### 2.6. Web 調査が必要な場合の経路

- 公式仕様・モデル既定値・provider 側の既知挙動などの grounded research は `gemini-cli-headless-delegation`（`tool_profile: "grounded_research"`、`timeout_sec >= 300`）を default 経路とする
- Claude 直接生成は fallback 経路とし、preflight 失敗 + 明示承認・既存コンテクスト充足のみで結論できる場合のいずれかに該当する場合のみ採用する
- repo 実体確認で確定可能な論点は `rg` / `find` で先に確認し、grounded_research を起動しない（トークン抑制）

### 3. Issue 本文を生成する

ステップ 0 で取得した必須セクション一覧に従い、implementation 種別では以下をすべて含める:

- `## Parent Issue` — 親 Issue 番号（なければ「なし（単独改善）」と明記）
- `## Machine-Readable Contract` — issue 種別ごとの required key を持つ YAML block
  - implementation / research: `contract_schema_version` / `issue_kind` / `parent_issue` / `goal_ref` / `change_kind`
  - parent: `contract_schema_version` / `issue_kind` / `goal_ref` / `change_kind` / `parent_mode` / `closure_mode`
  - 本文更新時は block 全体を削除せず、値だけを必要最小限で更新する
  - parent issue では `parent_mode` を `delivery-rollup` / `quality-gate` / `routing-map` / `decision-log` から選ぶ
  - `closure_mode` は `child-complete` / `measurement-ready` / `quality-validated` / `routing-complete` / `decision-recorded` の closed enum から選び placeholder のまま確定しない
  - `parent_mode` と `closure_mode` の互換は `delivery-rollup → child-complete` / `quality-gate → measurement-ready | quality-validated` / `routing-map → routing-complete` / `decision-log → decision-recorded` に固定する
  - `quality-gate` parent では `## Quality Decision Record` と `## Parent Closure Rule` を本文に残す
  - `<required: ...>` placeholder や enum 外値は missing と同様に invalid とし Issue 本文を確定させない
- `## Parent Goal Ref` — 親 Issue の `## Goal` または `## Outcome` を 1 回の読解で追える参照面
- `## Current Validated Scope` — parent tracker で narrow 済みの validated な作業範囲（今回の child issue で write-capable に扱う範囲だけ）
- `## Remaining Parent Gaps` — この child issue 完了後も parent に残る gap、または follow-up で追う項目
- `## Outcome` — 達成したい状態（1 文で明確に）
- `## In Scope` — 今回の PR で行うこと
- `## Out of Scope` — 今回の PR では行わないこと
- `## Acceptance Criteria` — チェックボックス形式の検証可能な条件
- `## Verification Commands` — **必須**。各 AC に対応するターミナルで実行可能なコマンドを列挙。Terminal AI Agent が自己完結で AC 検証を実施できるよう、`grep -n` / `pnpm typecheck && pnpm lint && pnpm test && pnpm build` 等の具体的なコマンドを含める。コマンドが 1 つも記載されていない Issue は不完全とみなす
- `## Allowed Paths` — 変更してよいファイル・ディレクトリの完全パス
- `## Stop Conditions` — **必須**。`.github/ISSUE_TEMPLATE/implementation.yml` の Stop Conditions セクションに記載された 6 定型項目をプレースホルダを埋めて記載
- `## Scope Delta（該当時のみ記載）` — Allowed Paths と実作業の乖離が生じた場合のみ
- `## Required Skills` — **ドメイン知識スキル**のみを記載する（例: TypeScript / ECS / Canvas / Vitest BDD 等）。ワークフロー skill（`issue-contract-review` / `implement-issue` / `pr-review-judge` / `ssot-discovery` 等）は書かない。runtime dependency がない場合は「なし」と明記するか省略
- `## Delivery Rule` — `1 Issue = 1 PR`、worktree 指定、Draft PR 既定など

#### VC 作成の決定論的ルール

- **削除確認パターン**: `grep "削除対象" <file> && echo "FAIL: 残存" || echo "PASS: 削除済み"`
- **marker 単位の独立確認**: 1 つの AC に複数の marker を使う場合、marker ごとにヒット件数を個別検証
- **決定論的判定**: `grep` / `rg` の exit code、`diff` の exit code、`pnpm test` の exit code、`test -f` / `test -d`、ファイルサイズ・行数の数値比較
- **意味的評価は VC に書かない**: 「コード品質の正当性」「算出値の妥当性」等は PR レビュアーの責務

#### Issue Template Guard（fail-closed）

本文ドラフト完成後、ステップ 0 の必須セクション一覧と照合する。不足セクションがあれば `[Issue Template Guard] Missing sections: <セクション名一覧>` を出力して Issue 生成を中断する。Stop Conditions セクションが空欄・1 項目のみの場合も不完全とみなす。

#### Machine-Readable Contract Guard（fail-closed）

`## Machine-Readable Contract` がない、または issue kind ごとの required key が欠ける場合は `[Issue Template Guard] Machine-Readable Contract keys are incomplete` を出力して中断する。

#### Required Skills Guard（fail-closed）

`## Required Skills` を書いた場合、各 bullet を以下の順で分類し、1 つでも違反があれば中断する:

1. **暗黙ワークフロースキル検出**: `issue-contract-review` / `implement-issue` / `pr-review-judge` / `ssot-discovery` などが含まれていたら `[Issue Template Guard] Required Skills contains implicit workflow skills` を出力して中断
2. **document / path reference 検出**: `docs/` / `.md` / `/` を含む repo path が含まれていたら `[Issue Template Guard] Required Skills contains document or path references; move them to ## Background or ## In Scope` を出力して中断
3. **canonical skill 名の照合**: current skill inventory にある canonical skill 名（bare の system skill と namespaced plugin skill の両方）はそのまま許容
4. **repo-local skill existence 確認**: bare skill 名を書く場合は `.claude/skills/<skill-name>/SKILL.md` の実在を確認。存在しない場合は中断
5. **runtime dependency なしの明示**: なければ「なし」へ正規化

### 3.5. Outcome Quality Guard（成果物形式・完了条件確認）

生成した Outcome が以下 2 要素を含むか確認する:

1. **成果物形式**: 何が出来上がるか（更新されたファイル、追加されたコミット、作成された PR、close 済み Issue など）
2. **完了条件**: 何をもって完了とするか（検証可能な状態）

**不適合パターン**（動作状態のみで成果物形式欠落）:
- 「〜が決定される」「〜が整理される」「〜が完了する」「〜が明確になる」「〜を検討する」「〜を改善する」

**判定基準**:
- **適合**: 成果物形式と完了条件の両方が明確。または受動的状態記述であっても具体的な成果物（ファイルパス・Issue 番号・PR 番号・コミット・リリース等）への参照が伴う
- **不適合**: 動作状態のみで「何が出来上がるか」「何をもって完了とするか」が曖昧

**不適合時**: `[Outcome Quality Guard] Outcome に成果物形式・完了条件が不足しています: <抽象表現>` を出力して中断、人間に具体的な書き換え案を提示する。

### 3.6. Allowed Paths ベースの類似 Issue 重複チェック（scope 重複チェック）

確定した `## Allowed Paths` の各パスについて OPEN Issue が存在するか確認する:

```bash
gh issue list --search "<file_path> is:open" --state open --json number,title,url
```

- OPEN Issue が 1 件以上 → false positive 除外を先に行い、本文への literal 含有を確認:
  ```bash
  gh issue view <N> --json body | python3 -c "import json,sys; b=json.load(sys.stdin)['body']; print('found' if '<file_path>' in b else 'not found')"
  ```
- literal 含有ありで scope 重複あり → 即座に停止し 3 択を提示
- scope 重複なし → 重複なし旨を人間確認事項に添えて次へ

#### 同一 Allowed Paths への複数 Issue 集約ガイドライン（マージコンフリクト回避）

scope 重複チェックの結果にかかわらず、以下を強く推奨する:

1. **1 PR への集約**: 同一ファイルへの複数の小変更 Issue を起票する場合は 1 つの実装 Issue にまとめて 1 PR で処理
2. **直列依存の設定**: 集約困難な場合は `## Delivery Rule` に依存関係を明記（例: `Depends on #<N> merge`）
3. **並行起票の禁止**: 同じファイルを Allowed Paths に含む複数の Implementation Issue を同時に OPEN 状態で並行起票・実装しない

### 4. 起票を実行する

Issue Template Guard / Outcome Quality Guard / Scope 重複チェックを全て通過したら、人間承認なしで即座に `.claude/skills/create-issue/scripts/create_issue_txn.py` を実行し、transaction として起票する。

helper は `--title` / `--body-file` / `--label` / `--parent-issue` / `--dependency` を受け取り、labels / sub-issue / dependency の read-back を同一 transaction で実施する。

**blocking stop**:
1. Scope が複数に分かれる場合（分割採否は人間判断）
2. Scope Overlap Detected で 3 択のアクション選択が必要な場合

上記以外の確認事項（調査で解決できる技術的事実・フラグ名・コマンド引数・ファイルパス等）は人間確認にせず、Issue 本文の `## Notes for Reviewer` セクションとして記録する。

`create_issue_txn.py` 実行後、起票した Issue URL（`issue_url`）を Output として提示する。

## Output

1. **Issue タイトル**: `<type>(<scope>): <description>` 形式で 1 案を決定（ユーザーへの選択肢提示は不要）
2. **Issue 本文案**: ステップ 3 の項目を含む完全な本文
3. **起票した Issue URL**（必須）
4. **分割案**（Scope が複数の場合のみ）
5. **Notes**（あれば）: blocking stop に該当しない補足事項

## 親子 Issue 構造ルール

サブ Issue を起票するときの不変条件:

**親 Issue = 共通文脈コンテナ**
- 背景・目的・調査結果・共有コンテキストのみを保持する
- 作業タスク（実装・修正・検証手順）は親 Issue 本文に書かない
- 既存の親 Issue 本文にタスクがある場合は、サブ Issue を追加する前にそれらをサブ Issue へ移行する

**サブ Issue = 作業タスク単位**
- `1 Issue = 1 PR` で完結する単一の作業タスクを持つ
- 親子の階層関係を表したい場合は GitHub native `sub-issues` を使う
- sibling issue 間で「どちらを先にマージしないと次へ進めないか」を表したい場合は `issue dependencies` を使う
- 単なる参考参照・closed precedent・将来候補の destination mapping だけで十分な場合は `sub-issues` / `issue dependencies` を増やさず、Issue 本文または comment に routing を残す

**sub-issue 登録手順**:

```bash
# 1. child issue の整数 databaseId を GraphQL で取得
CHILD_DB_ID=$(gh api graphql -f query='
{
  repository(owner: "{owner}", name: "{repo}") {
    issue(number: {child_number}) {
      databaseId
    }
  }
}' --jq '.data.repository.issue.databaseId')

# 2. parent issue に sub-issue として登録
gh api repos/{owner}/{repo}/issues/{parent_number}/sub_issues -X POST -F sub_issue_id=$CHILD_DB_ID
```

- `databaseId` 取得失敗時は POST に進まず fail-closed で停止
- 既存 child issue の親が一致しているかを read-back で確認:
  ```bash
  gh api repos/{owner}/{repo}/issues/{child_number}/parent --jq '.number'
  ```
- read-back が別の親を返した場合は `replace_parent=true` を使わず fail-closed で停止

**責務分担**:
- sub-issue の「登録手順」は本セクション（`create-issue`）が正本
- sub-issue として「登録済みかを確認する方法（read-back 確認）」は `issue-contract-review` が正本

## Blocker / Blocked-by 設定手順

### blocker 検出基準

- 対象機能の実装 PR が未マージ
- 依存 spec / 設計書が未確定
- 同一ファイルを変更する別 PR がオープンでコンフリクト可能性がある
- 本 Issue の AC が別 Issue の完了を条件としている

### 設定方法

```bash
python3 .claude/skills/create-issue/scripts/create_issue_txn.py \
  --repo <owner>/<repo> \
  --title "実装: <タイトル>" \
  --blocked-by <blocker_issue_number> \
  --blocked-by <another_blocker_number>
```

### 検証コマンド

```bash
# blocked-by 関係が登録されたことを GraphQL で確認
gh api graphql -f query='
query {
  repository(owner:"<owner>", name:"<repo>") {
    issue(number: <child_issue_number>) {
      blockedBy(first: 10) {
        nodes { number title }
      }
    }
  }
}'
```

`state/blocked` ラベルを付与し、blocker が解除されるまでキューに入れないことを推奨する。

## Partial-failure Recovery 手順

`create_issue_txn.py` がいずれかのステージで失敗した場合、Issue に partial-failure audit comment が自動投稿される。

### 失敗ステージ別の補正手順

| failed_stage | 補正手順 | idempotent |
|---|---|---|
| `sub-issue-readback` | readback で関係確認 → 未登録なら `sub_issues` 登録 | 既存関係 readback 後に再実行 |
| `dependency-readback` / `dependency-register` | blockedBy readback → 未登録なら GraphQL mutation | 既存関係 readback 後に再実行 |
| `label-readback` | `gh issue edit <N> --add-label <labels>` | yes |
| `dedupe-search` / `dedupe-race-detection` | 自動補正不可。手動で同タイトル open issue を確認・クローズ後に再実行 | no |

comment の "Recovery hint:" 以降に stage 固有の補正コマンドと idempotency 情報が記載されている。

## Guardrails

- 曖昧な要件を推測で埋めて Issue を確定させない
- `1 Issue = 1 PR` を超える Scope の Issue を単独で作成しない。黙って広げず、分割案を提示してから人間に確認する
- Acceptance Criteria に検証不可能な条件（「適切に動作すること」など）を含めない
- Verification Commands に実際に存在しないコマンド・ファイルを記載しない
- サブ Issue を起票・追加するとき、親 Issue 本文にタスクが残っていれば停止して移行案を提示する
- Outcome が動作状態のみで成果物形式を欠く Issue を確定させない（Outcome Quality Guard 参照）
- 未解決の追加調査が残る状態で、「確認する」「決める」だけを Outcome にした Issue を起票しない

## Related

- `.claude/skills/issue-body-authoring/SKILL.md` — 本文編集の shared skill（schema 定義・VC 作成ガイダンス）
- `.claude/skills/review-issue/SKILL.md` — Issue 品質レビュー
- `.claude/skills/issue-contract-review/SKILL.md` — 着手前 contract 確認
- `.claude/skills/ssot-discovery/SKILL.md` — 関連 SSOT の探索
- `.github/ISSUE_TEMPLATE/implementation.yml` / `parent.yml` / `research.yml` — Issue Forms 正本
