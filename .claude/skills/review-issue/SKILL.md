---
name: review-issue
description: Issue が Terminal AI Agent にとって「作業に迷わない・ハーネスエンジニアリング観点で再現可能」かを決定論的に判定するスキル。AC が検証可能か / Outcome に成果物形式と完了条件があるか / Verification Commands が実在コマンドのみ参照しているか / Stop Conditions 6 定型を満たすか / Required Skills の意味論を満たすか、を構造的にチェックする。issue-contract-review の前段として Issue 品質を整える。
---

# Review Issue

GitHub Issue が Terminal AI Agent にとって**作業に迷わない**か（コンテクスト・ハーネスエンジニアリング観点）を、決定論的に判定して修正差分提案を生成する。

評価の対象は **Issue 本文の構造的品質**であり、AC の動作検証や実装内容そのものは判定しない（それらは `pr-review-judge` / test-runner の責務）。

## Use When

- Issue を Terminal AI Agent が作業しやすいようレビューしたいとき
- 「Issue ◯◯ レビューして」「review issue」「Issue 確認して」などの短文トリガー
- `issue-contract-review`（実装前 contract 確認）の前段として Issue 品質を整えたいとき
- 新規 Issue の構造を整備したいとき

> 本 skill と `issue-contract-review` の使い分け（プロジェクトドキュメント `docs/dev/agent-skill-boundaries.md` に詳細）は、開発者が運用上参照するもので、本 SKILL.md 本文での再説明はコンテクスト汚染になるため省略している。

## Critical Guard: Issue refinement フェーズでは AC を実行しない

本 skill は **Issue refinement フェーズ（実装前の Issue 本文品質確認）** で呼び出される。以下を厳守する。

- **AC の Verification Commands を現行ファイル（実装前 baseline）に対して実行してはならない**
- AC は refinement 設計上「実装前 baseline で fail し、実装後に pass する」ことを前提とした検証スクリプトである。実装前に実行すれば fail するのが**正常動作**であり、これを「実装未着手」「needs-fix」と判定するのは**誤判定**
- レビュワーは **Issue refinement 観点（AC の検証可能性・baseline 失敗性・実装後 pass 可能性）のみ** を構造的に評価する:
  - AC が検証可能な形式（チェックボックス + 合否基準）で書かれているか
  - AC に対応する Verification Commands が「実装前 baseline で fail し、実装後に pass する」構造になっているか
  - Verification Commands が実在のコマンド・ファイルのみを参照しているか（静的検証のみ）

### アンチパターン（絶対に行わない）

- AC baseline fail を needs-fix と誤判定する（baseline fail は正常動作）
- AC を動作検証する（refinement では「検証可能性」を構造的に評価するのみ）
- baseline fail を理由に追加 iteration を要求する

## Procedure

### 事前判定: state/needs-human ラベル

`state/needs-human` ラベルが付いている Issue は人間判断待ちで AI 着手不可。本 skill では以下のみ判定:

- 人間が判断するための論点が `## Notes for Reviewer` / `## Stop Conditions` 等で明示されているか
- 上記以外は本文構造の品質チェックを軽量に行うのみで、AC/VC 詳細評価はスキップする（人間判断後に本文更新→再レビューする想定）

人間判断は別 Issue 化せず元 Issue 内で対応する運用のため（`human-confirm` テンプレは廃止済み）、種別は `parent` / `research` / `implementation` のいずれかとして判定する。

### レビュー手順

1. Issue 本文を読む:
   - Issue 種別（parent / research / implementation）を判定する
   - 対応するテンプレート `.github/ISSUE_TEMPLATE/{種別}.yml` を読み、必須セクション一覧（textarea labels）を取得する
   - 取得した必須セクション一覧の有無を Issue 本文で確認する
   - `## Required Skills` がある場合、エントリを以下のカテゴリに静的分類する:
     1. ワークフロー skill（`issue-contract-review` / `implement-issue` / `pr-review-judge` / `ssot-discovery` 等） — **Required Skills に書くべきではない**
     2. document / path reference（`docs/adr/...`, repo 内ファイルパス） — `Required Skills` ではなく `## Background` / `## In Scope` に書く
     3. ドメイン知識 skill（TypeScript / ECS / Canvas / Vitest BDD 等） — 適切

2. 確認項目を評価する（AI Agent が作業に迷わない・ハーネス engineering 観点）:

   以下はすべて **本文の構造を見て決定論的に判定** できる項目。AC の動作検証や実装内容の妥当性判定は本 skill のスコープ外。

   **構造・テンプレ整合**
   - **テンプレート準拠性**（Blocking）: 必須セクション（`.github/ISSUE_TEMPLATE/{種別}.yml` の textarea labels）がすべて存在するか
   - **Stop Conditions 妥当性**（Blocking、implementation 種別のみ）: `## Stop Conditions` が存在し、6 定型項目が記載され、プレースホルダが未記入の空欄がないか

   **Outcome 品質**（AI が成果物を生成できる粒度か）
   - **Outcome Abstraction**（Blocking）: Outcome が動作状態のみで成果物形式を完全に欠き、書き換え案の具体化に追加情報が必要なほど抽象的な場合（「〜を検討する」「〜を改善する」等）は blocking
   - **non-blocking improvement**: 成果物形式への参照が部分的にあり、軽微な具体化で適合できる場合
   - 不適合パターン例: 「〜が決定される」「〜が整理される」「〜を検討する」「〜を改善する」等、動作状態のみで成果物形式を欠く表現
   - **境界判定の目安**: AI が Issue 本文と既存文脈のみから書き換え案を自律生成できない場合は blocking、自律生成できる場合は non-blocking improvement

   **AC / VC 検証可能性**（AI が verify を機械実行できるか）
   - **AC 検証可能性**（Blocking）: チェックボックス形式で、合否が機械判定できる記述になっているか（「適切に動作する」等の主観表現は blocking）
   - **Verification 具体性**（Blocking）: ターミナルで実際に実行可能なコマンドが列挙されているか
   - **AC/VC 番号一致**（Blocking）: `# AC<N>` コメント番号が AC 番号と一致しているか

   **PR スコープ妥当性**（1 PR で完結し、レビュー・ロールバック可能か）
   - **単一意図**: Allowed Paths が 1 つの Outcome のためだけに必要なファイル群に閉じているか
   - **アーキ層のまとまり**: Allowed Paths が `src/state` / `src/render` / `src/systems` / `src/data` のいずれか 1 層に閉じているか、または層境界変更そのものが Outcome か
   - **ロールバック単位**: 1 PR を revert すれば Outcome が完全に元に戻るか
   - **In/Out Scope 衝突**: In Scope と Out of Scope に矛盾・重複がないか

   **Required Skills 意味論**（Blocking）
   - ワークフロー skill（`implement-issue`・`issue-contract-review`・`pr-review-judge`・`ssot-discovery` 等）が `## Required Skills` に含まれていない
   - document / path reference（`docs/adr/...`、repo 内ファイルパス）が `## Required Skills` に含まれていない
   - 実在しない skill 名が含まれていない
   - ドメイン知識 skill（TypeScript / ECS / Canvas / Vitest BDD 等）のみが列挙されている

   **その他**
   - **確認専用 Issue の禁止**（Blocking）: Outcome / AC / Stop Conditions を見て「確認する」「決める」「可否を調査する」だけが主目的で、実際にどの運用資産をどう更新して完了するかが書かれていない場合は `needs-fix`
   - **類似 Issue の重複**（non-blocking improvement）: 同一・類似 Outcome の OPEN Issue を `gh issue list --search "<keyword>" --state open` で確認し、重複候補があれば人間が方針を決定できるよう情報を提示

3. 判定する:
   - `approve`: AI Agent がそのまま着手できる
   - `needs-fix`: Blocking issues がある（修正が必要）

4. 差分提案を生成する:
   - `needs-fix` のときは、抽象評価で終わらせず Issue にそのまま反映できる本文更新案を出す
   - `approve` のときは本文更新提案や `gh issue edit` 実行前提の差分提案へ進まない。改善余地は `Non-blocking improvements` に任意提案として残す
   - 本文更新案は `追加すべき文` / `削除すべき文` / `書き換え案` の形式で示す

5. 本文更新の実施主体を分岐する:
   - `Verdict: approve` の場合は `invoked_as_loop` の値に関わらず本文更新へ進まない。レビュー結果のみ返して終了
   - `Verdict: needs-fix` かつ `invoked_as_loop: true`: 本文更新提案だけを返し、Issue 本文の更新は `issue-refinement-loop` / `issue-author` SubAgent 側へ委ねる
   - `Verdict: needs-fix` かつ `invoked_as_loop: false`: ユーザーに適用確認を行う

6. ユーザーに適用確認を行う（needs-fix + invoked_as_loop: false のみ）:
   - 差分提案を提示し、「この差分を Issue 本文に適用しますか？（yes/no）」と明示的に確認
   - ユーザーが承認するまで次のステップへ進まない
   - 拒否時は Issue 本文を変更せず skill を終了

7. 承認された差分を Issue 本文に適用する:
   - repo 配下 `tmp/` に修正後本文全体を書き出し、以下の guard を通してから `gh issue edit --body-file` を実行:
     ```bash
     mkdir -p tmp
     BODY_FILE="tmp/review-issue-<番号>-body.md"
     # 修正後の本文全体を $BODY_FILE に保存してから続行
     wc -c "$BODY_FILE"
     if [ "$(wc -c < "$BODY_FILE")" -le 1 ]; then
       echo "body-file が空または 1 byte です: $BODY_FILE" >&2
       exit 1
     fi
     if grep -Pn '\\(?:\"|\$)' "$BODY_FILE"; then
       echo "HEREDOC 由来のエスケープ混入か、正当な文字列リテラルの可能性があります" >&2
       exit 1
     fi
     gh issue edit <番号> --body-file "$BODY_FILE"
     ```
   - `--body-file` には修正後の本文全体を渡す（差分ではなく完全な本文）

8. 変更経緯を Issue にコメント投稿する:
   - 本文書き換え直後に以下を含むコメントを投稿:
     - 変更前箇所（セクション名と元の文面）
     - 変更後箇所（変更後の文面）
     - 変更理由（review-issue が指摘した理由）
     - 変更日時（ISO 8601）

## Output

- **Verdict**: `approve` / `needs-fix`
- **Blocking issues**: 修正しなければ着手できない問題（番号付き）
- **Non-blocking improvements**: あると良い改善（任意）
- **修正差分提案**: 追加すべき文 / 削除すべき文 / 書き換え案
- **人間への確認事項**: AI が判断できない点
- **適用結果**（承認・適用後）: 承認された差分と適用されたセクション一覧
- **コメント URL**（承認・適用後）: 投稿した変更経緯コメントの URL

## Validation Coverage Guard

E2E / live verification / research Issue をレビューする際、以下の 4 観点を分けて評価する。いずれかが欠落していれば `needs-fix`。

### 1. artifact 存在確認（verification）
- スクリーンショット・ログ・JSON 等の artifact が出力されることが確認可能か
- artifact の存在確認コマンドが AC / Verification Commands に含まれているか

### 2. 実行環境の確認（validation）
- 実行環境（Node / pnpm / Vite バージョン、ブラウザ等）が AC または Stop Conditions に明記されているか
- 環境依存の前提（ブラウザ表示・Canvas サポート等）が In Scope または Stop Conditions に記載されているか

### 3. 後続処理ハンドオフの確認（validation）
- 後続処理への引き渡し契約（出力ファイル名・データ形式等）が AC に含まれているか
- 中間 artifact だけでなく、後続コマンドへの接続まで確認できるか

### 4. 補助ツール vs 正規エントリポイントの確認（validation）
- AC・In Scope・Outcome の主体エントリポイントが `src/` 配下の正規モジュールになっているか
- 補助ツールが主体になっていないか（補助ツールは「開発補助として使用」と In Scope に記載）

## Guardrails

- 抽象論（「不明確です」）だけで終わらせず、必ず編集可能な文面（修正差分）を示す
- `issue-contract-review` の責務（実装前の contract 詳細確認）には踏み込まない
- Verdict が `approve` でも、Non-blocking improvements があれば提示する
- `approve` 判定時は `invoked_as_loop` の値に関わらず、本文更新提案・適用確認・`gh issue edit` 実行へ進まない
- `needs-fix` でも `invoked_as_loop: true` の場合は、本文更新提案だけを返し、本文更新は `issue-refinement-loop` / `issue-author` SubAgent 側へ委ねる
- 人間の明示的承認なく本文を書き換えない。承認後は必ず変更経緯コメントをセットで投稿する
- `gh issue edit` で本文を書き換える場合は repo 配下 `tmp/` の `--body-file` を使い、空/1 byte と HEREDOC 由来エスケープを事前 guard する

## Related

- [`.claude/skills/create-issue/references/body-authoring.md`](../create-issue/references/body-authoring.md) — Issue 本文更新の共通参照
- `.claude/skills/issue-refinement-loop/SKILL.md` — Issue 改善ループ（review-issue → issue-author への委譲先）
- `.claude/skills/issue-contract-review/SKILL.md` — 実装前 contract 確認
- `.claude/skills/ssot-discovery/SKILL.md` — Issue 関連 SSOT の探索
- `.github/ISSUE_TEMPLATE/implementation.yml` — 必須セクション一覧の正本
