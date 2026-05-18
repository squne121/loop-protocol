---
name: review-issue
description: Issue を Terminal AI Agent が安全・再現可能に着手できるか確認するときに使う。Outcome確認・In/Out Scope衝突検知・AC検証可能性・差分提案生成を行う。
---

# Review Issue

GitHub Issue の品質・Agent-friendliness を確認し、修正差分提案を生成するスキル。

## Use When

- Issue を Terminal AI Agent が作業しやすいようレビューしたいとき
- 「Issue ◯◯ レビューして」「review issue」「Issue 確認して」などの短文トリガー
- `issue-contract-review`（実装前 contract 確認）の前段として Issue 品質を整えたいとき
- 新規 Issue の構造を整備したいとき

> **責務分離**:
> - `review-issue`: Issue 自体の品質・Agent-friendliness を確認する（このスキル）
> - `issue-contract-review`: 実装前の contract（AC, Allowed Paths, 1PR判定）を確認する（別スキル）

## Critical Guard: Issue refinement フェーズでは AC を実行しない

本スキルは **Issue refinement フェーズ（実装前の Issue 本文品質確認）** で呼び出されるため、以下の制約を厳守すること:

- **AC の Verification Commands を現行ファイル（実装前 baseline）に対して実行してはならない。**
- AC は refinement 設計上「実装前 baseline で fail し、実装後に pass する」ことを前提とした検証スクリプトである。したがって実装前に実行すれば fail するのが**正常動作**であり、これを「実装未着手」「needs-fix」「実装が未完了」などと判定するのは**誤判定**である。
- レビュワーは **Issue refinement 観点（AC の検証可能性・baseline 失敗性・実装後 pass 可能性）のみ** を構造的に評価せよ。すなわち:
  - AC が検証可能な形式（チェックボックス + 合否基準）で書かれているか
  - AC に対応する Verification Commands が「実装前 baseline で fail し、実装後に pass する」構造になっているか
  - Verification Commands が実在のコマンド・ファイルのみを参照しているか（静的検証のみ）

### アンチパターン（絶対に行わない）

- **AC baseline fail を needs-fix と誤判定する**: Verification Commands を現行ファイルに対して実行し、fail したことを根拠に「実装が開始されていない」「needs-fix」と判定すること。これは refinement 設計の前提を理解しない誤判定であり、オーケストラレータの収束判定に雑音を加える。
- **AC を動作検証する**: refinement フェーズでは AC の「検証可能性」を構造的に評価するのみで、AC 自体の pass/fail を検証対象にしてはならない。
- **baseline fail を理由に追加 iteration を要求する**: baseline fail は正常動作のため、これを理由に issue 本文修正や追加レビューを求めない。

> 出典: Issue #732 iteration 2 で Haiku codex-task-delegator 経由の SubAgent がこの誤判定を起こし、ループ収束判定に雑音が発生した事例（Issue #754 で恒久化）。

## Procedure

### Decision issue 専用判定（必須）

このスキルは `implementation issue` のみをレビュー対象とするのではなく、`implementation` に見えない `decision-only issue` も検出して判定する。
以下を満たしたときのみ decision-only と扱う。

- `state/needs-human` が本文に含まれる、または `decision-only` / `意思決定 issue` の明示文が `Outcome` / `In Scope` / `Out of Scope` / `Handoff Contract` のいずれかにあること
- `## Next Action` が 1 つの実行可能アクションとして明記されていること
- `## Handoff Contract` が `記録先` / `参照先` / `次接続先` の 3 点を 1 箇所で明記していること

- **次に何をするかが不明確**（Next Action 不足または未読性）なら `needs-fix`（blocking）
  - 1行で `誰が/何を/なぜ` を特定できる `## Next Action` が必要
  - `候補比較`、`state/needs-human`、`次アクションの順序` が分からない場合は blocking
- **Handoff Contract 不足**（参照先/記録先不明）なら `needs-fix`（blocking）
  - `## Handoff Contract` は `記録先` と `参照先`、`次接続先（implementation issue / follow-up）` を 1 か所に明記
- **decision-only 解釈時の判定緩和**（重要）:
  - `decision-only` として判定した場合は、`## Next Action` と 3 点セットの `## Handoff Contract` が揃っていれば `Stop Conditions` 欠如のみで単体 blocking としない。
- **decision intent ありだが契約不足**
  - `state/needs-human` または `decision-only` / `意思決定 issue` の明示があるのに、`## Next Action` または `## Handoff Contract` の 3 点セットが欠ける場合は、`implementation` へフォールバックせず必ず `needs-fix`（blocking）
- **decision issue 誤読検出**（`implementation` として収束判定しない）
  - `## Next Action` があるのに `AC` が実装完了状態の説明のみで終わる場合、または本文が「最終決定をAIが下す」文面だけの場合は、まず上記 3 条件を欠いていないか確認し、欠ける場合は再提示を要求
- **decision-only 判定不足**
  - `state/needs-human` と明示ワードの両方がなく、`decision-only` の意図が読み取れない場合のみ `implementation` ルートで再評価する。
  - この fallback は decision intent が見えている malformed decision issue には使わない。

1. Issue 本文を読む:
   - Issue 種別（parent / research / human-confirm / implementation）を判定する。
   - 対応するテンプレートファイル `.github/ISSUE_TEMPLATE/github-ops-{種別}.md` を読み、必須セクション一覧を取得する。
   - 取得した必須セクション一覧の有無を Issue 本文で確認する。
   - `## Required Skills` がある場合は、entry を静的に分類する。
     1. 暗黙ワークフロースキル（`issue-contract-review` / `implement-issue` / `pr-review-judge` など）
     2. rule reference（`.agents/rules/...`、または `wsl-dev-environment` / `git-policy` などの rule slug）
     3. document / path reference（`.kiro/specs/...`, `design.md`, repo 内ファイルパス）
     4. current skill inventory にある canonical skill 名（bare の system skill と namespaced plugin skill を含む）
     5. current skill inventory にない bare repo-local skill 名（`.agents/skills/<name>/SKILL.md` の存在確認が必要）
   - **AI 製品設定変更 Issue の追加確認**: Claude Code / Codex CLI / Gemini CLI の model、model_reasoning_effort、personality、agent 設定、設定ファイル変更を扱うと読める場合は、Issue 本文の静的読解だけで済ませず、以下を read-only で確認する:
     1. `rg` / `find` で repo 実体を確認する（例: `.codex/config.toml`, `.codex/agents/*.toml`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`）。Issue が触れる設定面に対応する実体が本文に列挙されているかを見る。Codex 系設定を扱う場合は、少なくとも `.codex/config.toml` と存在する `.codex/agents/*.toml` の確認結果が別行で残っているか確認する。
     2. 必要な設定名を repo 実体と一次情報の両方で照合する。Codex CLI では `model` / `model_reasoning_effort` を優先確認し、`reasoning_effort` 単独名を正本扱いしない。
     3. `gh issue list` 等で同じ運用資産・設定ファイルを扱う open issue を確認し、Issue 本文の `## In Scope` / `## Out of Scope` に役割分担が反映されているかを確認する。
     4. 関連 open issue を根拠にしている場合、その issue の前提が current repo state と一致しているかを確認する。不一致なら stale issue として扱い、Issue 本文の `## Stop Conditions` に不一致内容が記載されているかを確認する。
2. 確認項目を評価する:
   - **テンプレート準拠性**: 対応するテンプレートの必須セクションがすべて存在するか（不足セクション名を具体的に列挙する）
   - **Outcome 明確性**: 1文で達成状態が伝わるか
   - **Outcome 抽象性（Outcome Abstraction）**: Outcome が成果物形式（何が出来上がるか：ファイル更新・PR・close 済み Issue 等）と完了条件（何をもって完了とするか）を明確に含むか。判定基準は create-issue の Outcome Quality Guard と共通（能動的な成果物形式への参照の有無）。
     - **blocking（needs-fix）昇格条件**: Outcome が動作状態のみで成果物形式を完全に欠き、かつ書き換え案の具体化に追加情報が必要なほど抽象的な場合（「〜を検討する」「〜を改善する」等、完了判定自体が不可能な Outcome）は `needs-fix` として blocking 扱いとする。
     - **non-blocking improvement**: Outcome に成果物形式への参照が部分的にあり、軽微な具体化で適合できる場合は、非ブロッキング改善として具体化提案を生成する。
     - 不適合パターン: 「〜が決定される」「〜が整理される」「〜が完了する」「〜が明確になる」「〜を検討する」「〜を改善する」等、動作状態のみで成果物形式を欠く表現。
     - **境界判定の目安**: 書き換え案を AI が Issue 本文と既存文脈のみから自律生成できない（外部情報の追加調査や人間の意思決定が必要）場合は blocking。既存情報から具体化案を自律生成できる場合は non-blocking improvement とする。
   - **In/Out Scope 衝突**: 矛盾・重複がないか
   - **1 Issue = 1 PR Scope**: 単一の目的・受入判定・ロールバック単位に収まるか（`issueops-common-guard.md` の Scope 規定を参照）
   - **AC 検証可能性**: チェックボックス形式で、合否が明確に判定できるか
   - **Verification 具体性**: ターミナルで実際に実行可能なコマンドか
   - **Allowed Paths 十分性**: 必要なファイルパスが網羅されているか
   - **Stop Conditions 妥当性（Blocking）**: 停止すべき状況が明確か。implementation 種別の Issue で以下のいずれかに該当する場合は `needs-fix`（Blocking）と判定する:
     - `## Stop Conditions` セクション自体が欠落している
     - 記載項目が 1 項目のみ（6 定型項目が未記載）
     - 定型項目のプレースホルダが未記入のまま（例: `<!-- 具体的なパスや状況を記載 -->` が残存する場合は許容するが、空欄はブロック）
   - **確認専用 Issue の禁止（Blocking）**: Outcome / AC / Stop Conditions を見て、「確認する」「決める」「可否を調査する」だけを主目的にしており、実際にどの運用資産をどう更新して完了するかが書かれていない場合は `needs-fix` と判定する。追加調査が必要なら Outcome ではなく `## Stop Conditions` に落とし込ませる。
   - **AI 製品設定変更 Issue の repo 実体確認（Blocking）**: Claude Code / Codex CLI / Gemini CLI の model、model_reasoning_effort、personality、agent 設定、設定ファイル変更を扱う Issue で、以下のいずれかに該当する場合は `needs-fix` と判定する:
     - `.codex/config.toml`、`.codex/agents/*.toml`、`AGENTS.md`、`CLAUDE.md`、`GEMINI.md` など repo 内の確認対象実体が In Scope / Background / AC に現れない
     - 設定名が repo 実体または一次情報とずれている。Codex CLI では `model` / `model_reasoning_effort` を正本候補として確認せず、`reasoning_effort` 単独名を前提にしている
     - 既存 open issue との役割分担が `## In Scope` / `## Out of Scope` に明記されておらず、同じ設定ファイルや運用資産への重複着手を防げない
     - `## In Scope` に repo reality の確認結果がなく、確認した実体名だけで「何を確認して何が存在したか」が残っていない
     - 関連 open issue を根拠にしているのに、その issue 番号・役割分類・current repo state との一致確認が本文に残っていない
     - 関連 open issue の前提が stale なのに、不一致内容と「その issue 単独を正本にしない」旨が `## Stop Conditions` に書かれていない
   - **曖昧さ**: 推測が必要な語句・条件が残っていないか
   - **類似 Issue の重複確認（non-blocking improvement）**: レビュー対象 Issue と同一または類似の Outcome を持つ OPEN Issue が存在しないかを確認する:
     ```bash
     # Issue タイトル・Outcome から代表キーワードを抽出して検索する
     gh issue list --search "<keyword>" --state open --json number,title,url
     ```
     - 類似 Issue が見つかった場合は、以下を判定してレビュー結果に添える:
       - **重複クローズ候補**: 同一 Outcome の既存 Issue が存在する場合、その Issue を重複クローズ候補として明示する
       - **既存 Issue への追記提案**: 類似するが完全重複でない場合は、既存 Issue に追記して対応する方法を提案する
     - 重複が確認された場合は `needs-fix` ではなく **non-blocking improvement** として扱い、人間が統合・新規起票の方針を決定できるよう情報を提示する
   - **Required Skills 意味論（Blocking）**: 「Runtime で呼ぶスキル」と「Implementer 参照スキル」が区分されているか。以下のいずれかに該当する場合は `needs-fix` と判定する。
     - 実装者向け参照スキル（`implement-issue`・`issue-contract-review`・`pr-review-judge` など）が誤って `## Required Skills` に含まれている
     - rule file / rule slug（`.agents/rules/...`, `wsl-dev-environment`, `git-policy` など）が `## Required Skills` に含まれている
     - spec / doc / path reference（`.kiro/specs/...`, `design.md`, repo 内ファイルパス）が `## Required Skills` に含まれている
     - current skill inventory にない bare skill 名が `.agents/skills/<name>/SKILL.md` と一致せず、実在しない
     - plugin / system skill が canonical 名ではなく、どの skill を指すか一意に読めない
3. 判定する:
   - `approve`: AI Agent がそのまま着手できる
   - `needs-fix`: Blocking issues がある（修正が必要）
4. 差分提案を生成する:
   - `needs-fix` のときは、抽象評価で終わらせず、Issue にそのまま反映できる本文更新案を出す。
   - `approve` のときは、本文更新提案や `gh issue edit` 実行前提の差分提案へ進まない。改善余地がある場合は `Non-blocking improvements` に任意提案として残す。
   - 本文更新案は `追加すべき文` `削除すべき文` `書き換え案` の形式で示す。
5. 本文更新の実施主体を分岐する:
   - `Verdict: approve` の場合は、`invoked_as_loop` の値に関わらず本文更新提案・適用確認・`gh issue edit` 実行へ進まない。レビュー結果のみ返して終了する。
   - `Verdict: needs-fix` かつ `invoked_as_loop: true` の場合は、本文更新提案だけを返し、Issue 本文の更新は `issue-refinement-loop` / `issue-body-authoring` 側へ委ねる。このスキル自身は適用確認や `gh issue edit` を実行しない。
   - `Verdict: needs-fix` かつ `invoked_as_loop: false` の場合のみ、ユーザーに適用確認を行う。
6. ユーザーに適用確認を行う:
   - ステップ 5 で `Verdict: needs-fix` かつ `invoked_as_loop: false` に該当した場合のみ、差分提案をユーザーに提示し、「この差分をIssue本文に適用しますか？（yes/no）」と明示的に確認する。
   - ユーザーが承認するまで次のステップへ進まない。
   - ユーザーが拒否した場合は、Issue本文を変更せずスキルを終了する。
7. 承認された差分をIssue本文に適用する:
   - ユーザーの承認が確認された場合のみ、repo 配下 `tmp/` に修正後本文全体を書き出し、以下の guard を通してから `gh issue edit --body-file` を実行する。
     ```bash
     mkdir -p tmp
     BODY_FILE="tmp/review-issue-<番号>-body.md"
     # 修正後の本文全体を $BODY_FILE に保存してから続行する
     wc -c "$BODY_FILE"
     if [ "$(wc -c < "$BODY_FILE")" -le 1 ]; then
       echo "body-file が空または 1 byte です: $BODY_FILE" >&2
       exit 1
     fi
     if grep -Pn '\\(?:\"|\$)' "$BODY_FILE"; then
       echo "HEREDOC 由来のエスケープ混入か、正当な文字列リテラルの可能性があります: $BODY_FILE" >&2
       echo "該当行を確認し、HEREDOC 由来なら修正、正当な literal なら確認メモを残してから再実行してください" >&2
       exit 1
     fi
     gh issue edit <番号> --body-file "$BODY_FILE"
     ```
   - `--body-file` には修正後の本文全体を渡す（差分ではなく完全な本文）。`/tmp` や `--body "<新本文全体>"` の inline 展開は使わない。
8. 変更経緯をIssueにコメント投稿する:
   - 本文書き換え直後に、以下のコマンドで変更経緯を記録する:
     ```
     gh issue comment <番号> --body "<変更経緯>"
     ```
   - コメント本文には以下を含める:
     - **変更前箇所**: 変更されたセクション名と元の文面
     - **変更後箇所**: 変更後の文面
     - **変更理由**: `review-issue` スキルが指摘した理由
     - **変更日時**: ISO 8601 形式（例: `2026-04-08T10:00:00+09:00`）

## Output

- **Verdict**: `approve` / `needs-fix`
- **Blocking issues**: 修正しなければ着手できない問題（番号付き）
- **Non-blocking improvements**: あると良い改善（任意）
- **修正差分提案**:
  - 追加すべき文
  - 削除すべき文
  - 書き換え案
- **人間への確認事項**: AI が判断できない点
- **適用結果**（承認・適用後）: 承認された差分と適用されたセクション一覧
- **コメントURL**（承認・適用後）: 投稿した変更経緯コメントのURL
## Required Skills

**Runtime スキル**: なし（本スキル自身が review-issue として呼ばれるため、他の runtime スキル呼び出しはない）

**Implementer 参照スキル**:
- `issue-body-authoring` — Issue 本文更新案と issue-author handoff の参照
- `issue-contract-review` — 実装前 contract 確認（実装者向け参照）


## Validation Coverage Guard

E2E・live verification・research Issue をレビューする際、「artifact 存在確認（verification）」と「実行環境・後続処理・補助ツール vs 正規エントリポイントの確認（validation）」を分けて評価する。以下のチェックリストを適用し、いずれかが欠落している場合は `needs-fix` とする。

### Validation Coverage チェックリスト

#### 1. artifact 存在確認（verification）
- [ ] スクリーンショット・ログ・JSON 等の artifact が出力されることが確認可能か
- [ ] artifact の存在確認コマンドが AC / Verification Commands に含まれているか

#### 2. 実行環境の確認（validation）
- [ ] 実行環境（OS / WSL2 / Python バージョン / uv 仮想環境 等）が AC または Stop Conditions に明記されているか
- [ ] 環境依存の前提（GUI 表示・UIA アクセス・デバイス接続 等）が In Scope または Stop Conditions に記載されているか

#### 3. 後続処理ハンドオフの確認（validation）
- [ ] 後続処理への引き渡し契約（`meta.json` / `path_reason` / `ingest_result.json` 等）が AC に含まれているか
- [ ] 中間 artifact だけでなく、後続コマンドへの接続（例: `src/main.py ingest` の完了）まで確認できるか

#### 4. 補助ツール vs 正規エントリポイントの確認（validation）
- [ ] AC・In Scope・Outcome の主体エントリポイントが `wip/` スクリプト（補助ツール）になっていないか
- [ ] `wip/` スクリプトが補助ツールとして正しく位置づけられ、正規エントリポイント（`src/main.py` 等）が Outcome の主語になっているか

### wip/ 主体エントリポイント記述の CC-SDD SSOT 違反フラグ

Issue の `## Outcome` / `## In Scope` / `## Acceptance Criteria` のいずれかで `wip/` 配下のスクリプト（例: `wip/run_e2e_with_uia_dumps.py`、`wip/kindle_ingestion/xxx.py` 等）が **正規エントリポイント**（Outcome の達成主体・AC の検証主体）として記述されている場合は、CC-SDD SSOT 違反として `needs-fix` を返す。

**判定基準**:
- `wip/` スクリプトが「このスクリプトで E2E を実行する」「この補助ツールで検証する」等の表現で Outcome / AC の主体になっている → SSOT 違反
- `wip/` スクリプトが「開発補助・デバッグ用途」として In Scope に記載されており、Outcome の主体が `src/` 配下のモジュールや正規 CLI である → 適切（違反ではない）

**修正差分提案の形式**:
```
[SSOT違反] wip/ スクリプトが Outcome / AC の主体エントリポイントとして記述されています。
- 対象: <該当箇所の文面>
- 理由: wip/ は補助ツールであり、CC-SDD SSOT 規約上、Outcome / AC の主体は src/ 配下の正規エントリポイントとする必要があります（.agents/rules/issueops-common-guard.md 参照）。
- 修正案: Outcome / AC を正規エントリポイント（例: src/main.py ingest）を主体として書き直し、wip/ スクリプトは「開発補助として使用」として In Scope に追記する。
```

> 出典: Issue #1281 で `wip/run_e2e_with_uia_dumps.py` が正規エントリポイントとして記述され、スクリーンショット単体の artifact 確認を E2E バリデーション完了と誤判定した事例（Issue #1323 child Issue B として #1295 の知見を統合）。

## Guardrails

- 抽象論（「不明確です」）だけで終わらせず、必ず編集可能な文面（修正差分）を示す。
- `issue-contract-review` の責務（実装前の contract 詳細確認）には踏み込まない。
- Verdict が `approve` でも、Non-blocking improvements があれば提示する。
- `approve` 判定時は `invoked_as_loop` の値に関わらず、本文更新提案・適用確認・`gh issue edit` 実行へ進まない。
- `needs-fix` でも `invoked_as_loop: true` の場合は、本文更新提案だけを返し、Issue 本文の更新は `issue-refinement-loop` / `issue-body-authoring` 側へ委ねる。
- 人間の明示的承認なく本文を書き換えない。承認後は必ず変更経緯コメントをセットで投稿する。
- `gh issue edit` で本文を書き換える場合は、repo 配下 `tmp/` の `--body-file` を使い、直前に `wc -c "$BODY_FILE"` と `grep -Pn '\\(?:\"|\$)' "$BODY_FILE"` を実行して空/1 byte ファイルと HEREDOC 由来エスケープ混入の要確認行を表示する。ヒット時は即続行せず、HEREDOC 由来なら修正し、正当な文字列リテラルなら確認メモを残してから再実行する。
- AI 製品設定変更 Issue をレビューするとき、repo 実体確認不足や設定名の誤りを external research 不足だけの問題として矮小化しない。local reality check 不足として blocking 扱いにする。
- AI 製品設定変更 Issue の根拠を、issue comment や口頭共有に依存させない。repo reality の確認結果・関連 open issue の役割分担・stale 前提の扱いは issue body の `In Scope` / `Out of Scope` / `Stop Conditions` に残っていない限り blocking 扱いにする。
- E2E / live verification / research Issue をレビューする際、artifact 存在確認（verification）だけで validation 充足と判断しない。Validation Coverage Guard の 4 観点（artifact / 実行環境 / 後続処理ハンドオフ / 補助ツール vs 正規エントリポイント）をすべて確認してから verdict を出す。

## Related

- rule: `.agents/rules/issueops-common-guard.md`
- rule: `.agents/rules/github-ops-workflow.md`
- skill: `.agents/skills/issue-body-authoring/SKILL.md`
- skill: `.agents/skills/issue-refinement-loop/SKILL.md`
- skill: `.agents/skills/issue-contract-review/SKILL.md`
- skill: `.agents/skills/issueops-operations/SKILL.md`
- issue-template: `.github/ISSUE_TEMPLATE/github-ops-implementation.md`
- issue-template: `.github/ISSUE_TEMPLATE/github-ops-parent.md`
- issue-template: `.github/ISSUE_TEMPLATE/github-ops-research.md`
- issue-template: `.github/ISSUE_TEMPLATE/github-ops-human-confirm.md`
