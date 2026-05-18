---
name: create-issue
description: ユーザーの要求を Terminal AI Agent が再現可能に作業できる GitHub Issue に整形するときに使う。要求分析・Scope判定・Issue本文生成・即時起票を行う。blocking stop（Scope分割採否・Scope Overlap 3択）以外は人間承認なしで `scripts/github_ops/create_issue_txn.py` を実行する。
---

# Create Issue

ユーザーの要求を分析し、Terminal AI Agent が安全・再現可能に着手できる GitHub Issue を生成するスキル。

## Use When

- ユーザーが要求を GitHub Issue として起票したいとき
- 「Issue 起票して」「Issue 作って」「Issue にまとめて」「create issue」などの短文トリガー
- 要求を `1 Issue = 1 PR` で完結する Scope に整理したいとき
- `research` / `implementation` のどちらかを見分けてテンプレートを選びたいとき

## Follow-up Canonical Entrypoint

follow-up 起票の canonical contract は `.agents/skills/shared-agent-skills-governance/references/follow-up-issue-contract.md` を正本とする。`create-issue` はその contract を実行する entrypoint であり、`issue-refinement-loop` / `post-merge-cleanup` などのオーケストレーターは follow-up 候補の抽出に責務を限定する。

## Orchestrator Input Contract

必須フィールド、`desired_destination` / `validated_scope_delta` guard、`ISSUE_AUTHOR_COVERAGE_V1`、`proposal_only` boundary は `.agents/skills/shared-agent-skills-governance/references/follow-up-issue-contract.md` を参照する。`create-issue` では contract を緩和せず、そのまま fail-closed で適用する。

## Procedure

0. テンプレートを読み込む（Issue Template Guard）:
   - Issue 種別（parent / research / human-confirm / implementation）を判定する。
   - `human-confirm` は、**未確定の人間判断を GitHub 上で非同期に保留する必要がある場合だけ**選ぶ。main conversation で即時に聞ける質問、または追加調査で埋められる論点には使わない。
   - タイトル先頭に `調査:` が必要か、`実装:` が必要かを先に確認し、research-vs-implementation を誤らないようにする。
   - 対応するテンプレートファイル `.github/ISSUE_TEMPLATE/github-ops-{種別}.md` を読み、`## ` で始まる **Markdown 見出し行**（YAML frontmatter と HTML コメント内の行は除外）を必須セクション一覧として取得する。
   - ステップ3の本文生成ではこの必須セクション一覧を基準にする。
1. 要求を分析する:
   - ユーザーの要求から Outcome（達成したい状態）を抽出する。
   - **anchor 主張を含む Issue**: Issue 本文で「既存ファイルの行番号・セクション見出し・関数名」を anchor として主張する場合は、起票前に `issue-body-authoring` の `## Anchor Verification Preflight` を参照し、`git grep` / `rg` で hit 件数を確認してから起票する。
   - **follow-up Issue の場合（post-merge-cleanup / issue-refinement-loop から委譲）**: issue-body-authoring ガイドラインの「ワークフロー不具合検出時の修正方針起案ガイダンス」セクションを参照し、決定論的修正と workaround を明示比較してから Outcome を起案する。根本原因の決定論的修正を第一候補とし、prompt 注記 workaround は次点として扱う。
   - 実装案だけでなく、運用で解決できる案も比較し、採用方針を明示する。
   - 要件が曖昧な場合は Issue を確定させず、`## Notes for Reviewer` に記載するか blocking stop として扱う（推測で埋めない）。
   - **タイトル prefix と AC の性質をセルフチェックする**: `research` / `調査` を名乗る Issue に `src/` や `tests/` の実装変更が AC として入っていないか確認する。入っている場合は `implementation` / `実装` に切り替えるか、Scope を分割して別 Issue にする。
   - 不確実性が残る場合は `.agents/rules/issue-uncertainty-policy.md` を参照し、`phase/research` / `state/needs-human`（移行期は `state/needs-investigation` 互換）の付与要否を先に決める。implementation に昇格できる場合は `実装:` prefix + `phase/implementation` + `state/queued` の canonical ready tuple を正本として付与する。
   - **human-confirm routing guard**:
     1. **人間意図が固定済み**: 明示指示、人間コメント、または人間が確認した Human Intent Ledger/comment URL で方向性が既に決まっている場合は、同じ論点を `human-confirm` に戻さない。#1905 型の「既に決まったことを再度決めてもらうためだけの Issue」は起票禁止。
     2. **調査不足**: repo reality、official docs、既存 Issue/PR コメントの確認不足で未確定に見えている場合は、まず `codebase-investigator` / `web-researcher` に回す。事実確認不足を人間判断待ちにすり替えない。
     3. **main conversation 質問**: 現在の会話で答えを聞けば足りる質問、または GitHub に pending decision を残す必要がない質問は、新規 Issue ではなく main conversation に返す。回答を得たら、続行前に元 Issue/PR comment へ `decision` / `answer` / `source conversation` / `supersedes` を記録する。
     4. **human-confirm 許可条件**: 追加調査を完了してもなお未確定の人間判断だけが残り、その判断を GitHub 上に非同期で残して後続作業の待機点にする必要がある場合に限って `human-confirm` を許可する。
   - **desired destination handoff guard**:
     1. `issue-refinement-loop` から follow-up 候補を受ける場合、各候補の `desired_destination` と `validated_scope_delta` を必須とする。どちらかが欠落している場合は起票せず、`failure_reason: destination mapping required` で委譲元へ返す。
     2. 必須フィールドが揃っている場合は、Issue 本文の `## Background` または `## In Scope` に反映し、narrow な current validated scope だけで新規 Issue を再定義しない。
     3. `desired_destination` があるのに routing 先 Issue 本文から消えている場合は、silent に起票せず `failure_reason` 付きで委譲元へ返し、destination mapping required として扱う。
     4. `desired_destination` をそのまま write-capable claim や scope 拡張に昇格させず、repo reality と一次情報で validated な範囲だけを `## In Scope` に入れる。
   - **research follow-up 起票前の repo reality check（ツール CLI / output contract / artifact path）**:
     follow-up 起票候補が `output` / `artifact` / `CLI` の現実差分を含む場合は、`ready` を付けない。
     1. 対象ツール CLI が repo reality で実行可能かを確認する（`which` / `type` / 実行コマンド）。
     2. `analysis_output/` や `reports/...` など output contract / artifact path を repo 実体と照合し、期待と実体が一致することを確認する。
     3. `--help` / `ls ... || echo` のみで満足判定しない。実行結果が生成先と整合しない場合は、research issue を ready にしない。
     4. repo reality 不一致を検知した場合は、先に implementation child issue を起票して差分修正を行い、research issue は blocked-by として分離する。
   - **sweep / cleanup issue 起票前ガード**:
     1. タイトル、Outcome、Verification Commands のいずれかに `sweep` / `cleanup` / `残存` / `repo 全体` / `rg -n --hidden -g '!.git'` のような repo-wide inventory シグナルがある場合は、通常の issue 起票前に「起票前 dry-run」を必須化する。
     2. dry-run では、Issue 本文へ入れる予定の Verification Commands、またはそれと同値の read-only コマンドを実際に実行し、`remaining_count` と `sample_hits` を取得する。`sample_hits` は最大 3 件までとし、0 件の場合は `なし` と記録する。
     3. `remaining_count == 0` の場合は **新規 issue を起票しない**。`Issue 作成結果` の代わりに「対象 0 件のため起票不要」と報告し、実行コマンド、残存件数 0、サンプルなしを返して終了する。
     4. dry-run でヒットがあっても、同じ concern を扱う既存 open issue が comment-first / destination mapping 上で見つかった場合は **新規 issue を起票しない**。canonical destination（Issue 番号と comment URL）を返し、「既存 issue へ統合」を報告して終了する。
     5. 「失敗ログから新規 issue を作る」「外部 AI が新規 issue を提案した」ケースでは、ログ文言の一致だけで新規起票を決めず、関連 issue の本文に加えて comments / destination mapping / current repo reality を確認してから重複判定する。
     6. 起票中止時の報告は少なくとも以下を含める:
        - `decision`: `skip_issue_creation` / `route_to_existing_issue`
        - `remaining_count`
        - `sample_hits`
        - `inspected_commands`
        - `canonical_destination`（既存 issue に統合した場合のみ）
   - **AI 製品設定変更を含む要求**（Claude Code / Codex CLI / Gemini CLI の model、model_reasoning_effort、personality、system prompt、agent 設定、設定ファイル変更など）の場合は、Issue 本文生成前に以下を確認する:
     1. **repo 内実体確認**: 対象設定の repo 内実体を `rg` / `find` で確認する。例: `.codex/config.toml`、`.codex/agents/*.toml`、`AGENTS.md`、`CLAUDE.md`、`GEMINI.md`。
     2. **設定名の正規化**: repo 実体と一次情報の両方を見て、Issue 本文では正式な設定名を使う。Codex CLI では `model` と `model_reasoning_effort` を優先確認し、`reasoning_effort` 単独名を正本扱いしない。
     3. **既存 open issue 照合**: 同じ運用資産や設定ファイルを扱う open issue がないか確認し、役割を `重複` `依存` `後続実装` `参考のみ` に分類する。分類結果は Issue 本文の `## In Scope` / `## Out of Scope` に issue 番号付きで残す。
     4. **repo reality の本文固定**: Issue 本文の `## In Scope` に、Issue が触れる設定面に対応する repo 実体を列挙し、確認結果を 1 行ずつ残す。例: ``- `.codex/config.toml` を確認し、Codex CLI の設定キーは `model` / `model_reasoning_effort` を使用している``。実体名だけを書いて確認結果を書かない本文は不完全とみなす。Codex 系設定を扱う場合は、少なくとも `.codex/config.toml` と、存在する `.codex/agents/*.toml` の確認結果を別行で残す。
     5. **related issue の一致確認固定**: 関連 open issue を本文に残す場合、各 issue について current repo state との照合結果も本文に残す。少なくとも「#N は current repo state と一致確認済み」または「#N の前提 X は stale のため、この issue 単独を正本にしない」のどちらかを残す。
     6. **stale issue の扱い**: 関連 open issue の前提が current repo state と食い違う場合、その issue を根拠に Scope を切らず、不一致内容を `## Stop Conditions` に明記する。少なくとも「どの issue のどの前提が古いか」と「この issue 単独を正本にしない」ことを残す。
     7. **追加調査の扱い**: repo 実体確認や一次情報確認をしても未解決の論点が残る場合、その論点を Outcome にせず `## Stop Conditions` に落とし込む。`確認する` / `決める` だけを主目的にした Issue は作らない。
1.5. 類似 Issue のキーワードベース重複チェック（起票前検索）:
   - ステップ1で抽出した Outcome・タイトル案から代表キーワード（2〜3語）を選ぶ。
   - 以下のコマンドで OPEN Issue を検索する:
     ```bash
     # <keyword> にはタイトル案・Outcome から抽出した代表キーワードを入れる
     gh issue list --search "<keyword>" --state open --json number,title,url
     ```
   - 例: Outcome が「sync-skills recipe への [unix] variant 追加」の場合:
     ```bash
     gh issue list --search "sync-skills unix variant" --state open --json number,title,url
     ```
   - 結果の解釈:
     - OPEN Issue が0件 → 重複なし。次のステップへ進む。
     - OPEN Issue が1件以上 → タイトル・本文を確認し、scope 重複の有無を判定する:
       - **comment-first 確認を追加する**: タイトル・本文が近い Issue は、判定前に comments を確認し、`destination mapping` / `統合記録` / `source mapping` / `canonical destination` の有無を調べる。本文が古くても、comments で既存 open issue への統合先が確定している場合は、その comment を正本として扱う。
       - **canonical destination が確定している場合**: 人間選択へ進まず、`decision: route_to_existing_issue` と `canonical_destination` を返して **その場で停止**する。3択は `destination mapping` / `canonical destination` が未確定な場合にだけ提示する。
       - **重複あり（同一または極めて近い Outcome を持つ OPEN Issue が存在する）**: **即座に新規起票を中止**し、以下の情報を人間に提示して既存 Issue への統合を提案する:
         ```
         [類似 Issue 検出] 以下の OPEN Issue が同一または類似の Outcome を持つ可能性があります:
         - #<number>: <title> (<url>)
         新規起票を中止します。以下のアクションを選択してください:
         1. 既存 Issue (#<number>) に追記して対応する（既存 Issue 本文へのコメント追記案を提示）
         2. 既存 Issue を重複クローズ候補として指定し、新規 Issue に統合する
            → AI の後続動作: 新規 Issue に「Closes #<number>（重複）」を記載して起票する
         3. scope が異なることを確認し、新規 Issue を起票する（理由を明記）
         ```
       - **重複なし（タイトル・本文・comment-first 確認を行っても scope が明確に異なり、canonical destination も未確定）**: 重複なし旨を人間確認事項に添えて次のステップへ進む。
       - **false positive の除外**: GitHub Full-Text Search はキーワードをトークン分割するため、ヒット結果に false positive が含まれうる。ヒット Issue の本文・タイトルを目視確認し、明らかに無関係のものは除外してから判定する。

2. Scope を判定する:
   - `1 Issue = 1 PR` で完結する Scope かを確認する（`issueops-common-guard.md` の Scope 規定を参照）。
   - AC は検証可能な記述にし、実装か調査かを自分で確認する。研究寄りなら `調査:`、実装寄りなら `実装:` をタイトル先頭に置く。
   - Scope が複数に分かれる場合は、分割案と各 Issue の Outcome を提示して人間に確認する。
2.5. `proposal_only` で Issue 本文案を受ける場合の caller-side 境界を固定する:
   - Gemini CLI に下書きだけを委譲したい場合は、`gemini-cli-headless-delegation` の wrapper へ `tool_profile: proposal_only` と `output_sections: ["issue_authoring_draft"]` を明示する。
   - 返却された `issue_authoring_draft` は **proposal text** として扱い、そのまま GitHub に投稿済みの本文や確定済み outcome とみなさない。
   - final file edit / shell edit / GitHub mutation は引き続き Codex 側 worker または main thread が保持する。Gemini を default write-capable と扱ってはならない。
   - request に `post_to_issue_url`、direct file edit、shell execution、GitHub mutation 指示が混ざる場合は fail-closed とし、caller 側で request を修正してから再実行する。

2.6. AI 製品設定変更 Issue の fact-finding（公式仕様 / 既知挙動）で Web 調査が必要な場合の経路既定（Gemini default / Claude 直接生成 fallback）:
   - 公式仕様・モデル既定値・provider 側の既知挙動など grounded research が必要な fact-finding は、Gemini headless delegation（`gemini-cli-headless-delegation` 経由、`tool_profile: "grounded_research"`、`timeout_sec >= 300`）を **default 経路**とする。Claude 直接生成（main conversation や create-issue caller が直接生成すること）は **fallback 経路**とし、preflight 失敗 + 明示承認・既存コンテクスト充足・repo 実体確認のみで結論できる場合のいずれかに該当する場合のみ採用する。
   - 注: `proposal_only` で十分な場合（Issue 本文案の生成のみ）は上記 2.5 の経路を使う（grounded_research を起動しない）。`external_research: skipped` 判定は LOOP_STATE を持つ orchestrator skill（`impl-review-loop` / `issue-refinement-loop`）専用で、create-issue では適用外。
   - 設定キー名・既定値などすでに repo 実体確認（`.codex/config.toml` / `.codex/agents/*.toml` など）で確定可能な論点には grounded_research を起動せず、repo 実体確認を優先する（消費トークン抑制）。
   - 詳細な caller-side routing 規約は `.agents/skills/web-researcher/SKILL.md` を参照する。
3. Issue 本文を生成する:
   - ステップ0で取得した必須セクション一覧に従い、implementation 種別の場合は以下をすべて含める:
     - `## Parent Issue` — 親 Issue 番号（なければ「なし（単独改善）」と明記）
     - `## Machine-Readable Contract` — issue 種別ごとの required key を持つ YAML block
       - implementation: `contract_schema_version`, `issue_kind`, `parent_issue`, `goal_ref`, `change_kind`
       - research: `contract_schema_version`, `issue_kind`, `parent_issue`, `goal_ref`, `change_kind`
       - human-confirm: `contract_schema_version`, `issue_kind`, `parent_issue`, `goal_ref`, `decision_type`
       - parent: `contract_schema_version`, `issue_kind`, `goal_ref`, `change_kind`, `parent_mode`, `closure_mode`
       - `change_kind` と `decision_type` は machine-readable routing 用のため block に置く
       - `## Required Skills` と `## Rules` は注釈付き prose section を正本とし、block へ二重化しない
       - 本文更新時は block 全体を削除せず、値だけを必要最小限で更新する
       - parent issue では `parent_mode` を `delivery-rollup` / `quality-gate` / `routing-map` / `decision-log` から選ぶ
       - `closure_mode` は `child-complete` / `measurement-ready` / `quality-validated` / `routing-complete` / `decision-recorded` の closed enum から選び、placeholder のまま確定しない
       - `parent_mode` と `closure_mode` の互換は `delivery-rollup -> child-complete`、`quality-gate -> measurement-ready | quality-validated`、`routing-map -> routing-complete`、`decision-log -> decision-recorded` に固定する
       - `quality-gate` parent では `## Quality Decision Record` と `## Parent Closure Rule` を省略しない。`#2027` を precedent として、`measurement-ready / quality-unvalidated` と `quality-validated` の区別を本文に残す
       - `quality-gate` parent では `closure_mode` と `Quality Decision Record.Status` を同一編集で揃える。`measurement-ready` は `measurement-ready / quality-unvalidated`、`quality-validated` は verdict と `Decision Date` / evidence 付きの QDR を前提とする
       - `<required: ...>` placeholder や enum 外値は missing と同様に invalid とし、Issue 本文を確定させない
       - `#446` の runtime guard が未実装な間は、この contract は self-enforcing ではない。quality-gate parent を body 更新だけで close-ready と扱わない
     - `## Parent Goal Ref` — 親 Issue の `## Goal` または `## Outcome` を 1 回の読解で追える参照面。parent tracker がある場合は `Desired Destination` もここに要約し、child issue が何のための 1 PR かを固定する
     - `## Current Validated Scope` — parent tracker で narrow 済みの validated な作業範囲。将来 destination と混同せず、今回の child issue で write-capable に扱う範囲だけを書く
     - `## Remaining Parent Gaps` — この child issue 完了後も parent に残る gap、または follow-up / destination mapping で追う項目。残件がなければ「なし」と明記してよい
     - `## Outcome` — 達成したい状態（1文で明確に）
     - `## In Scope` — 今回の PR で行うこと
       - AI 製品設定変更 Issue では、repo 実体確認の結果を具体的に記載する。実体名だけでなく「何を確認し、何が repo に存在したか」を 1 行以上で残す。Issue が触れる設定面に対応する repo 実体を列挙し、Codex 系設定を扱う場合は `.codex/config.toml` と存在する `.codex/agents/*.toml` を別行で残す。
     - `## Out of Scope` — 今回の PR では行わないこと（Follow-up Issue 案を添える）
       - AI 製品設定変更 Issue では、関連 open issue を issue 番号付きで列挙し、役割（`重複` `依存` `後続実装` `参考のみ`）を明記する。さらに current repo state と一致確認済みかどうかを各 issue ごとに残す。
     - `## Acceptance Criteria` — チェックボックス形式の検証可能な条件
     - `## Verification Commands` — **必須**。各 AC に対応するターミナルで実行可能なコマンドを列挙する。Terminal AI Agent が自己完結で AC 検証を実施できるよう、`grep -n`・`just check` 等の具体的なコマンドを含める。コマンドが1つも記載されていない Issue は不完全とみなす。
     - コードブロック内に `\` で行継続するコマンドがある場合は、貼り付け事故を避けるため `--body-file` 形式や別ファイル参照を優先する。
     - `## Allowed Paths` — 変更してよいファイル・ディレクトリの完全パス
     - `## Stop Conditions` — **必須**。`.github/ISSUE_TEMPLATE/github-ops-implementation.md` の Stop Conditions セクションに記載された 6 定型項目（Allowed Paths 外の変更 / 固定契約変更 / 新規 Issue 起票 / 後続 Phase 波及 / nested SubAgent delegation / 外部サービス・権限昇格・既存テスト大規模改変）をプレースホルダを埋めて記載する。空欄・1項目のみは不完全とみなす。
       - AI 製品設定変更 Issue では、関連 open issue の前提が current repo state と食い違う場合、その不一致内容と「この issue 単独を正本にしない」旨を Stop Conditions に残す。
     - `## Scope Delta（該当時のみ記載）` — Allowed Paths と実作業の乖離が生じた場合のみ記載
     - `## Rules` — GitHub Ops implementation issue では省略せず、少なくとも `.agents/rules/github-ops-workflow.md` と `.agents/rules/issueops-common-guard.md` を既定記述として含める。必要に応じて `.agents/rules/file-edit-protocol.md` など issue 固有の rule を追加する。本セクション明示により、実装者・SubAgent が自動的にルール参照を行う仕組みが有効化される。
     - `## Required Skills` — **runtime dependency のみ**を記載する（`issue-contract-review` / `implement-issue` / `pr-review-judge` はすべての implementation child issue に暗黙的に必要なワークフロースキルであり、ここには列挙しない。詳細: `.agents/rules/github-ops-workflow.md` KH-N6）。rule file は `Required Skills` ではなく `## Rules` に記載する。`.kiro/specs/...` や `.md` ファイル、repo 内パスなどの document / path reference も `Required Skills` には入れず、`## Background` / `## In Scope` / `## Rules` の適切な section に置く。runtime dependency がない場合は「なし（runtime dependency なし）」と明記するか省略する
     - `## Delivery Rule` — `1 Issue = 1 PR`、worktree 指定、Draft PR 既定など
   - research 種別の場合は以下を追加で設定する:
     - `## Allowed Paths` — 以下の2段テンプレートから実態に合うものを選択して記載する:
       - **読み取り専用の場合**（repo ファイルへの write 操作なし）:
         ```
         - 読み取り専用（repo ファイル変更なし）
         - Issue コメント投稿・本文更新は許可（`gh issue comment` / `gh issue edit`）
         ```
       - **write 操作を含む場合**（実測実験・sandbox 実行など）:
         ```
         - <対象ファイル・ディレクトリの完全パスを列挙>
         - Issue コメント投稿・本文更新は許可（`gh issue comment` / `gh issue edit`）
         ```
     - `## Stop Conditions` — **実測実験（codex exec 経路・sandbox 実行など）を含む research Issue には必須**。以下の最小セットを記載する:
       ```
       - Allowed Paths 外への write 操作が発生した場合は即座に停止する
       - GitHub API エラーが2連続した場合は即座に停止し、人間に報告する
       - 実験スクリプトが予期しないファイル生成・削除を行った場合は即座に停止する
       ```
       読み取り専用の research Issue でも Stop Conditions セクションを省略しない。最低限「Allowed Paths 外への write 操作が発生した場合は即座に停止する」を記載すること。
   - 追加対象のリソースが実際に存在するか検証が必要な場合は `grep -n` で確認してから AC に記載する（存在しないメソッド・ファイルへの参照は不可）。
   - **VC の `rg` パターン構文検証（rg 構文チェック）**: `## Verification Commands` を生成した直後に以下を実行する:
     1. VC のコマンド列に `rg` が含まれるかを確認する。
     2. `rg` コマンドの正規表現パターンに `\|` が含まれる場合、以下の手順で修正要否を判定する:
        a. そのパターンが **複数キーワードの OR 検索**を意図しているかを確認する（例: `rg "foo\|bar"` で「foo または bar を含む行」を検索したい場合は OR 検索の意図がある）。
        b. OR 検索を意図している場合のみ `\|` を `|` に修正する（`\|` は GNU grep の拡張構文であり `rg` では無効なリテラル `\|` として扱われる）。
        c. リテラルのバックスラッシュ+パイプを意図している場合（例: sed / awk のエスケープされたパイプ文字列を検索する VC）は修正しない。
        d. 判別が曖昧な場合は修正せず、「OR 検索か literal `\|` 検索か」を人間確認事項に追加する。
     3. 修正例:
        ```bash
        # 誤（rg では \| はリテラル文字列 "\|" にマッチする。OR 検索を意図している場合は修正が必要）
        rg "foo\|bar" file.md
        # 正（rg の OR は | を使う）
        rg "foo|bar" file.md
        # 修正不要の例（sed パターン中のリテラル \| を grep したい場合）
        rg "s/foo\|bar/baz/" script.sh
        ```
     4. OR 検索として修正を行った場合は「VC 構文修正: `\|` → `|`（rg の OR 演算子）」を人間確認事項に添える。
   - **関数スコープ限定 VC パターン（Python AST ベース）**: AC に「特定の関数内で特定の依存/記述を確認する」が含まれる場合、`grep` はファイル全体を対象とするため関数スコープを保証できない。代わりに以下の AST ベースパターンを使う:
     ```bash
     python3 -c "
     import ast, sys, re
     src = open('<file>').read()
     tree = ast.parse(src)
     found = False
     for node in ast.walk(tree):
         if isinstance(node, ast.FunctionDef) and node.name == '<target_func>':
             found = True
             body_src = ast.get_source_segment(src, node) or ''
             hits = [l for l in body_src.splitlines()
                     if re.search(r'<pattern>', l)
                     and not l.lstrip().startswith('#')
                     and '\"\"\"' not in l and \"'''\" not in l]
             if hits:
                 print('FAIL:', hits); sys.exit(1)
             else:
                 print('OK: <condition>')
     if not found:
         print('FAIL: <func> not found'); sys.exit(1)
     "
     ```
     **必須の 3 要素**: (1) `found = False` フラグ（関数が存在しない場合の検出）、(2) コメント行除外（`not l.lstrip().startswith('#')`）、(3) docstring 除外（`'\"\"\"' not in l and \"'''\" not in l`）。この 3 要素を省略すると偽陰性・偽陽性が発生する。
     **使用場面**: AC に「`<func>` 関数内で `<dependency>` を使用していないこと」や「`<func>` 内の `<old_code>` が削除されていること」を含む場合。
   - **削除確認パターン**: AC に「旧記述が削除されていること」を含む場合、`grep` の失敗を合否判定に使う:
     ```bash
     grep -n "削除対象の記述" <file> && echo "FAIL: 旧記述が残存しています" || echo "PASS: 旧記述が削除済みです"
     ```
     `&&` と `||` の組み合わせにより、grep 成功（残存）= FAIL、grep 失敗（削除済み）= PASS を表現できる。
   - **marker 単位の独立確認パターン（重要）**: 1 つの AC に複数の marker を使う場合、marker ごとにヒット件数を個別に検証して `合否` を分離する。
     ```bash
     # 各 marker の出現数を個別算定
     MARKER_A_COUNT=$(rg -n "## marker: X" .agents/skills/open-pr/SKILL.md | wc -l)
     MARKER_B_COUNT=$(rg -n "## marker: Y" .agents/skills/open-pr/SKILL.md | wc -l)

     if [ "$MARKER_A_COUNT" -ne 1 ]; then
       echo "FAIL: marker A は 1 件想定（actual=$MARKER_A_COUNT）"
     else
       echo "PASS: marker A"
     fi

     if [ "$MARKER_B_COUNT" -ne 1 ]; then
       echo "FAIL: marker B は 1 件想定（actual=$MARKER_B_COUNT）"
     else
       echo "PASS: marker B"
     fi
     ```
   - **test-runner 向け決定論的 VC と PR レビュアー向け意味的評価 AC の分離**：
     VC はすべて **決定論的（deterministic）** な形式で作成すること。意味的評価（セマンティック）は PR レビュアーの責務であり、test-runner が実行可能な VC 内で行わせない。
     - **決定論的判定（test-runner が実行可能）**:
       - `grep` / `rg` の exit code（パターンが存在するか否か）
       - `diff` の exit code（ファイルが一致するか否か）
       - `pytest` / `just check` の exit code（全テスト合格か否か）
       - ファイル存在確認（`test -f` / `test -d`）
       - ファイルサイズ・行数の数値比較
     - **意味的評価（PR レビュアーが判定）**:
       - コード品質の正当性（「このコードは計画通り正しいコードか」等）
       - 算出値の妥当性（「この数値は期待値として適切か」等）
       - ドメイン固有の正当性（「この業務ロジックは正しいか」等）
     - **例：AC が「OCR 出力が改善されていること」の場合**:
       - 誤り（意味的評価を test-runner に要求）: `bash scripts/live-verify.sh ... | grep "OCR 精度" | grep -v "前："` （grep hit の有無で「改善されている」を判定しようとしている）
       - 正解（決定論的）: `bash scripts/live-verify.sh ... && echo "PASS: live-verify 実行成功" || echo "FAIL: live-verify 実行失敗"` （VC は実行結果の成否のみを判定）
       - 意味的評価は PR レビュアーが「実行結果の出力値を目視し、期待される改善が実現されているか」を判定する
   - **Issue Template Guard（fail-closed）**: 本文ドラフト完成後、ステップ0の必須セクション一覧と照合する。不足セクションがあれば `[Issue Template Guard] Missing sections: <セクション名一覧>` を出力して Issue 生成を中断する。Stop Conditions セクションが空欄・1項目のみの場合も不完全とみなし `[Issue Template Guard] Stop Conditions: 6 定型項目の記載が必要です` を出力して中断する。
   - **Machine-Readable Contract Guard（fail-closed）**: `## Machine-Readable Contract` がない、または issue kind ごとの required key が欠ける場合は `[Issue Template Guard] Machine-Readable Contract keys are incomplete` を出力して中断する。`Required Skills` / `Rules` を block に移し替えて prose section を削除する案は reject する。
   - **Required Skills Guard（fail-closed）**: `## Required Skills` を書いた場合、各 bullet を次の順で分類し、1 つでも違反があれば Issue 生成を中断する。
     1. **暗黙ワークフロースキル検出**: `issue-contract-review` / `implement-issue` / `pr-review-judge` など実装者向け参照スキルが含まれていたら `[Issue Template Guard] Required Skills contains implicit workflow skills: <entries>` を出力して中断する。
     2. **rule reference 検出**: `.agents/rules/` パス、または `wsl-dev-environment` / `git-policy` のような rule slug が含まれていたら `[Issue Template Guard] Required Skills contains rule references; move them to ## Rules: <entries>` を出力して中断する。
     3. **document / path reference 検出**: `.kiro/specs/`, `.md`, `/` を含む repo path、`design.md` / `requirements.md` のような参照先が含まれていたら `[Issue Template Guard] Required Skills contains document or path references; move them out of ## Required Skills: <entries>` を出力して中断する。
     4. **canonical skill 名の照合**: current skill inventory にある canonical skill 名は、その表記をそのまま許容する。これには bare の system skill（例: `openai-docs`, `skill-creator`）と namespaced plugin skill の両方を含む。
     5. **repo-local skill existence 確認**: current skill inventory にない bare skill 名を書く場合は `.agents/skills/<skill-name>/SKILL.md` の実在を確認する。存在しない場合は `[Issue Template Guard] Required Skills references unknown skills: <entries>` を出力して中断する。
     6. **曖昧表記の reject**: canonical skill inventory にない path 風表記や曖昧な略称は reject する。
     7. **runtime dependency なしの明示**: runtime dependency がないのに placeholder 的に skill 名を書かず、「なし（runtime dependency なし）」へ正規化する。
   - `proposal_only` から `issue_authoring_draft` を受け取った場合でも、Codex 側で本文を見直し、必須セクション・VC・destination mapping を補ってから final file edit / GitHub mutation を行う。
   - **body-file 指向**: コードブロックに `\` 末尾の行継続が含まれる場合、Markdown の折返しやシェルの解釈ずれを避けるため、本文直書きより `--body-file` を優先する。
   - **AI 製品設定変更 Issue Guard（fail-closed）**: AI 製品設定変更を含む implementation Issue では、以下のいずれかを満たさない場合に `[Issue Template Guard] AI settings issue evidence is incomplete` を出力して中断する:
     1. `## In Scope` に、Issue が触れる設定面に対応する repo 実体と確認結果が列挙されている
     2. `## Out of Scope` に、関連 open issue の issue 番号・役割分類・current repo state との一致確認結果がある
     3. 関連 open issue の前提が stale な場合、その不一致内容が `## Stop Conditions` に書かれている
3.5. Outcome Quality Guard（成果物形式・完了条件確認）:
   - ステップ3で生成した Outcome について以下2要素が含まれるか確認する:
     1. **成果物形式**: 何が出来上がるか。例：
        - 「`.agents/rules/X.md` に Y ルールが追記された」
        - 「Issue #N が close され、対応する PR #M が merge された」
        - 「テンプレートファイル `.github/ISSUE_TEMPLATE/Z.md` が更新された」
     2. **完了条件**: 何をもって完了とするか（検証可能な状態）。例：
        - 「`rg` で参照可能」
        - 「CI が通って PR が merge された」
        - 「ファイルの該当セクションに記述が追加された」
   - **不適合パターン（動作状態のみで成果物形式欠落）**:
     - 「〜が決定される」「〜が整理される」「〜が完了する」「〜が明確になる」「〜を検討する」「〜を改善する」
   - **判定基準**:
     - **適合**: 成果物形式と完了条件の両方が明確。または、受動的状態記述（「〜が決定される」「〜が整理される」等）であっても、具体的な成果物（ファイルパス・Issue 番号・PR 番号・コミット・リリース等）への参照が伴う。
     - **不適合**: 動作状態のみで「何が出来上がるか」「何をもって完了とするか」が曖昧。Outcome に能動的な行為・成果物形式（更新されたファイル、追加されたコミット、作成された PR、close 済み Issue など）への参照が含まれない。
   - **不適合時の動作**:
     1. `[Outcome Quality Guard] Outcome に成果物形式・完了条件が不足しています: <抽象表現>` を出力する
     2. Issue 生成を中断する
     3. 人間に具体的な成果物形式・完了条件を含む書き換え案を提示する

3.6. Allowed Paths ベースの類似 Issue 重複チェック（scope 重複チェック）:
   - ステップ3で `## Allowed Paths` を確定した後、各パスについて OPEN Issue が存在するかを確認する:
     ```bash
     # <file_path> は Allowed Paths の各エントリに置き換える
     gh issue list --search "<file_path> is:open" --state open --json number,title,url
     ```
   - 例: `Allowed Paths` が `.agents/skills/create-issue/SKILL.md` の場合:
     ```bash
     gh issue list --search ".agents/skills/create-issue/SKILL.md is:open" --state open --json number,title,url
     ```
   - 結果の解釈:
     - OPEN Issue が0件 → そのまま次のステップへ進む。
     - OPEN Issue が1件以上 → **false positive の除外を先に行う**:
       - GitHub の Full-Text Search はパスをトークン分割するため、ヒット結果には false positive が含まれうる。ヒット Issue の本文に Allowed Paths の完全文字列が literal として含まれるかを以下で確認する:
         ```bash
         # <N> はヒットした Issue 番号、<file_path> は Allowed Paths の各エントリに置き換える
         gh issue view <N> --json body | python3 -c "import json,sys; b=json.load(sys.stdin)['body']; print('found' if '<file_path>' in b else 'not found')"
         ```
       - literal 含有なしの Issue は false positive とみなして除外する。
       - literal 含有ありの Issue についてのみ、scope 重複がないかをタイトル・本文で確認する:
         - scope 重複あり（同一ファイルへの変更を含む OPEN Issue が存在する）: **即座に停止**し、以下の情報を人間に提示して確認を求める:
           ```
           [Scope Overlap Detected]
           新規 Issue の Allowed Paths "<file_path>" に対して、以下の OPEN Issue が同一ファイルを変更対象としている可能性があります:
           - #<number>: <title> (<url>)
           アクション（いずれかを選択してください）:
           1. 既存 Issue に統合する
              → AI の後続動作: 新規 Issue 起票をキャンセルし、既存 Issue への追記案を提示してステップ4（人間確認事項の提示）へ進む。
           2. 既存 Issue の完了後に着手する（今回は起票のみ）
              → AI の後続動作: 今回の起票を継続し、`## Delivery Rule` に依存 Issue を追記してステップ4へ進む。
           3. scope が異なることを確認し、新規 Issue を起票する（理由を明記）
              → AI の後続動作: 相違理由を人間確認事項に記録し、ステップ4（人間確認事項の提示）へ進む。
           ```
         - scope 重複なし（同一ファイルでも変更箇所が明確に異なる）: 重複なし旨を人間確認事項に添えて次のステップへ進む。

   **同一 Allowed Paths への複数 Issue 集約ガイドライン（マージコンフリクト回避）**:

   scope 重複チェックの結果にかかわらず、以下の状況では複数の小変更 Issue を1つの PR に集約することを強く推奨する:

   - 同じファイルを Allowed Paths に含む複数の Issue を並行して起票・実装すると、個別 PR が同一ファイルを別々に変更し、マージコンフリクトが発生するリスクが高い。
     - 実事例: Issue #964 / #965 / #966 がすべて `.kiro/specs/kindle-content-ingestion/design.md` をターゲットにした小変更だったが別々の PR になり、PR #970 / #971 マージ後に PR #976 でマージコンフリクトが発生した。
   - **推奨方針**:
     1. **1 PR への集約**: 同一ファイルへの複数の小変更 Issue を起票する場合は、1つの実装 Issue にまとめて1 PR で処理することを推奨する。
     2. **直列依存の設定**: 集約が困難な場合は、先行 Issue の PR マージ後に後続 Issue の実装を開始するよう `## Delivery Rule` に依存関係を明記する（例: `Depends on #<N> merge`）。
     3. **並行起票の禁止**: 同じファイルを Allowed Paths に含む複数の Implementation Issue を同時に OPEN 状態で並行起票・実装しない。一方がマージされるまで他方の実装開始を保留する。
   - この方針は scope 重複チェックで「scope 重複なし」と判定された場合でも適用する（変更箇所が異なっていても、同一ファイルへの並行変更はマージコンフリクトを引き起こしうる）。

4. 起票を実行する:
   - Issue Template Guard・Outcome Quality Guard・Scope 重複チェックを全て通過したら、人間承認なしで即座に `scripts/github_ops/create_issue_txn.py` を実行し、transaction として起票する。
   - helper は `--title` / `--body-file` / `--label` / `--parent-issue` / `--dependency` を受け取り、labels / sub-issue / dependency の read-back を同一 transaction で実施する。
   - **blocking stop（人間確認が引き続き必要なもの）**:
     1. Scope が複数に分かれる場合（分割採否は人間判断）
     2. Scope Overlap Detected で3択のアクション選択が必要な場合
  - 上記以外の確認事項（調査で解決できる技術的事実・フラグ名・コマンド引数・ファイルパス等）は人間確認にせず、Issue 本文の `## Notes for Reviewer` セクションとして記録する（人間が後でレビュー）。
  - `create_issue_txn.py` 実行後、起票した Issue URL（`issue_url`）を Output として提示する。

## Follow-up false-ready 回避（#1900/#1908 の実例）

- `--help` または `ls ...` のみで artifact の存在有無を決めず、`analysis_output/` と `reports/` の実体差分を確認してから調査起票する。
- `repo reality` で期待 CLI の出力先が一致しない場合は、research issue を ready 扱いしない。先に implementation child issue を切って、`## Stop Conditions` に不一致理由を明記する。
- `#1900` / `#1908` の false pass 事例は、`not yet created` 判定だけで stop する例としてテンプレートと実例で残す。

## Output

1. **Issue タイトル**: `<type>(<scope>): <description>` 形式で1案を決定して使用する（ユーザーへの選択肢提示は不要）
2. **Issue 本文案**: Procedure 手順3の項目を含む完全な本文
3. **起票した Issue URL**（必須）: `create_issue_txn.py` 実行後に取得した `issue_url`
4. **分割案**（Scope が複数の場合のみ）: 各 Issue の Outcome と分割理由
5. **Notes**（あれば）: blocking stop に該当しない補足事項（調査で解決できなかった技術的事実・後でレビューを要する観点等）

## 親子 Issue 構造ルール

サブ Issue を起票するとき、または既存 Issue にサブ Issue を追加するときは以下を必ず確認・実施する:

**親 Issue = 共通文脈コンテナ**
- 背景・目的・調査結果・共有コンテキストのみを保持する
- 作業タスク（実装・修正・検証手順）は親 Issue 本文に書かない
- 既存の親 Issue 本文にタスク（`- [ ]` チェックボックスや手順ステップ）がある場合は、サブ Issue を追加する前にそれらをサブ Issue へ移行する

**サブ Issue = 作業タスク単位**
- `1 Issue = 1 PR` で完結する単一の作業タスクを持つ
- 親 Issue の Outcome・共通コンテキストを参照する
- 親子の階層関係を表したい場合は GitHub native `sub-issues` を使う。親 Goal を前進させる child issue をぶら下げる場面が対象
- sibling issue 間で「どちらを先にマージしないと次へ進めないか」を表したい場合は `issue dependencies` を使う。親子ではなく順序拘束だけが必要な場合は body/comment routing より依存関係メタデータを優先する
- 単なる参考参照、closed precedent、将来候補の destination mapping だけで十分な場合は `sub-issues` / `issue dependencies` を増やさず、Issue 本文または comment に routing を残す
- parent tracker から child issue を切る場合は、親本文の `## Goal` / `## Desired Destination` / `## Current Validated Scope` / `## Remaining Parent Gaps` を読み、child 側の `## Parent Goal Ref` / `## Current Validated Scope` / `## Remaining Parent Gaps` へ要約転記する
- `desired destination` を child issue の `## In Scope` へそのまま昇格させず、repo reality と parent 本文で validated な範囲だけを `## Current Validated Scope` と `## In Scope` に入れる
- parent issue を新規起票・更新する場合は、`parent_mode` と `closure_mode` を block にだけ置いて終わらせず、本文の `## Quality Decision Record` / `## Parent Closure Rule` にも close 契約を prose で残す。`quality-gate` parent は `#2027` のように child close 数ではなく Quality Decision Record の確定で close する
- `closure_mode` の prose 表現は key と分離し、本文では `measurement-ready close` のような自然言語を補足に使ってよいが、machine-readable key は `measurement-ready` / `quality-validated` / `child-complete` / `routing-complete` / `decision-recorded` のいずれかに固定する
- `quality-gate` parent で `closure_mode` と `Quality Decision Record.Status` が不一致な本文は invalid とし、close-ready にしない
- `parent_mode` と互換しない `closure_mode` の組み合わせ（例: `quality-gate + child-complete`）も invalid とし、close-ready にしない
- `<required: ...>` placeholder や enum 外値が残る本文も invalid とし、missing と同様に fail-close で止める
- `## Parent Issue` に `#<親Issue番号>` を記載してサブ Issue を新規起票した場合は、起票直後に GitHub sub-issue 関係を登録する
  ```bash
  # 1. child issue の整数 databaseId を GraphQL で取得する
  CHILD_DB_ID=$(gh api graphql -f query='
  {
    repository(owner: "{owner}", name: "{repo}") {
      issue(number: {child_number}) {
        databaseId
      }
    }
  }' --jq '.data.repository.issue.databaseId')

  # 2. parent issue に sub-issue として登録する
  gh api repos/{owner}/{repo}/issues/{parent_number}/sub_issues -X POST -F sub_issue_id=$CHILD_DB_ID
  ```
  - child issue 作成直後に `child_number` と `child_url` を保持し、sub-issue 登録の成否にかかわらず人間が追跡できる状態にする
  - `databaseId` 取得が失敗した場合、または `CHILD_DB_ID` が空 / null の場合は `sub_issues` POST に進まず fail-closed で停止する。保持済みの `child_number` と `child_url` を人間へ報告し、再開時は新規 child issue を再起票せず、既存 child issue から `databaseId` を再取得する
  - `sub_issue_id` には issue number ではなく整数の `databaseId` を渡す（`gh issue view --json id` が返す node ID は使わない）
  - `sub_issues` 登録だけが失敗した場合は、新規起票フロー全体をやり直さず、まず既存 child issue の親が一致しているかを child 側 API で read-back 確認する
    ```bash
    gh api repos/{owner}/{repo}/issues/{child_number}/parent --jq '.number'
    ```
  - read-back の結果が `parent_number` と一致した場合は成功扱いにして終了する
  - read-back が `200` で別の親を返した場合は `replace_parent=true` を使って親を置き換えず、fail-closed で停止する。保持済みの `child_number` と `child_url`、返ってきた親 Issue 番号を人間へ報告し、手動判断を待つ
  - read-back が `404` / `410` / その他の非 `200` の場合は「未紐付け確定」とみなして自動再試行せず、fail-closed で停止する。保持済みの `child_number` と `child_url` を添えて、既存 child issue を再利用した手動再開を人間へ報告する
  - 自動 retry loop は作らない。再開時は新規 child issue を追加起票せず、既存 child issue を再利用する

**責務分担**
- sub-issue の「登録手順」はこのセクション（`create-issue`）が正本。
- sub-issue として「登録済みかを確認する方法（read-back 確認）」は `issue-contract-review` スキルの Step 4 が正本。
- この 2 スキル間の手順に差異が生じた場合は、このセクションを優先して `issue-contract-review` 側を修正する。

**適用タイミング**
- `create-issue` でサブ Issue を新規起票するとき
- `issueops-operations` Review To Issue で親子関係を設定するとき
- `issueops-operations` で既存の親 Issue にサブ Issue を追加するとき
- Issue 本文を編集して親子構造に変換するとき

**違反時の対応（fail-closed）**
- 親 Issue 本文にタスクが残っている状態でサブ Issue を追加しようとした場合は停止する
- 「親 Issue 本文のタスク一覧」と「サブ Issue 化の移行案」を人間に提示してから進める

## Guardrails

- 曖昧な要件を推測で埋めて Issue を確定させない。
- `1 Issue = 1 PR` を超える Scope の Issue を単独で作成しない。黙って広げず、分割案を提示してから人間に確認する。
- Acceptance Criteria に検証不可能な条件（「適切に動作すること」など）を含めない。
- Verification Commands に実際に存在しないコマンド・ファイルを記載しない。
- サブ Issue を起票・追加するとき、親 Issue 本文にタスクが残っていれば停止して移行案を提示する（親子 Issue 構造ルール参照）。
- Outcome が動作状態のみで成果物形式を欠く Issue を確定させない（詳細: Procedure ステップ3.5 Outcome Quality Guard）。不適合と判定した場合は Issue 生成を中断する。
- **実測実験（codex exec 経路・sandbox 実行など）を含む research Issue には Stop Conditions 必須**。Stop Conditions セクションが空欄または省略されている research Issue は不完全とみなし、最低限の Stop Conditions 最小セット（Procedure ステップ3の research 種別ガイダンス参照）を追記してから確定させる。
- AI 製品設定変更を含む Issue を、repo 実体確認なしで external research 先行のまま確定させない。`.codex/config.toml` や `.codex/agents/*.toml` などの現物確認で埋められる論点は先に埋める。
- AI 製品設定変更を含む Issue で、repo reality の確認結果や関連 open issue の役割分担を issue body に残さず、コメントや人間の記憶に逃がさない。
- 未解決の追加調査が残る状態で、「確認する」「決める」だけを Outcome にした Issue を起票しない。必要なら Stop Conditions / follow-up issue に分離する。
- fixed human intent を、`human-confirm` Issue に再包装しない。人間コメントや明示指示で方向性が固定済みなら、その意図を正本として Issue 本文へ反映する。
- repo reality / official docs / 既存 Issue コメントの確認不足を `state/needs-human` に丸投げしない。まず追加調査へ回し、GitHub Issue 化が不要な質問は main conversation で聞く。
- `human-confirm` Issue は、追加調査完了後も未確定の人間判断が残り、その判断待ちを GitHub 上で非同期に保持する必要がある場合だけ起票する。

## Human意図固定 / 調査不足 / main conversation / human-confirm の分岐

create-issue では、`human-confirm` を乱発しないため、以下を必ず分岐する:

1. **fixed human intent**
   - 明示指示・`humanコメント`・人間確認済み ledger/comment URL で方針が固定済みの場合は `human-confirm` へ戻さない
   - 固定済み内容は `Outcome` / `In Scope` / `Out of Scope` に反映し、実装に入れる
2. **research gap（調査不足）**
   - `repo reality` / 公式仕様 / 既存 Issue/PR の確認不足が残る場合は、まず `codebase-investigator` と `web-researcher` を実行して事実を埋める
   - 事実未確定を `human-confirm` で代理済ませない
3. **main conversation質問**
   - 追加調査後、回答内容が1点の方向性確認だけで会話上で即時解決できる場合は、main conversation で聞く
   - 回答を得たら、元の Issue/PR comment に `decision` / `answer` / `source conversation` / `supersedes` を返してから再開する
   - GitHub 上で非同期待ちを作る前提がないなら新規 Issue を起票しない
4. **真の human-confirm Issue**
   - 追加調査を完了しても、未解決が「人間判断のみ」でなお GitHub 非同期で決裁待ちが必要な場合のみ許可
   - `human-confirm` テンプレートへ移す際は、未決の判断ポイントを1項目ずつ明記する

再発事例防止の原則: #1905 のように「すでに固定済みの意図を再度確認するだけの Issue」を起票しない。

## 追加ガイダンス（create-issue）

### fixture / spec-contract fixture 対応の Allowed Paths ガイダンス

- `fixture` と `spec-contract fixture` を編集する実装案件では、`Allowed Paths` に対象の fixture/spec-contract fixture を明示する。  
- `spec-contract` 変更に伴う `fixture` 側の更新要否がある場合は、`create-issue` の契約面で同時に許可範囲を示す。
- `Allowed Paths` は「最終的に触れるファイル」を明示し、後段の `issue-contract-review` で fixture/spec-contract 漏れが再確認できる形式にする。

### doc/contract 修正時の同一ファイル内残存パターン確認ガイダンス

- doc/contract（`rules`/`skills`/`issue template` など）を修正する際は、残存する類似誤記や同種表現の再発チェックを AC に組み込む。
- `Verification Commands` には同一ファイル内の残存パターン確認を検索できる `grep` 系コマンドを 1 つ以上入れる。  
  例: `grep -n "類似残存\|同一ファイル内\|同種誤記\|残存パターン" <target_file>`

## doc-lint baseline 取り扱い

Issue 本文に doc-lint baseline ファイルへの言及を含める場合は、以下の正本ファイル名を使う。

### 正本ファイル名

| ファイル | 目的 |
|----------|------|
| `.doc-lint.baseline.json` | doc-lint の WARNING/ERROR 抑制（fingerprint baseline）。`just lint-docs` / `doc_lint.cli check ... --update-baseline` で生成。**こちらが fingerprint 抑制の正本。** |
| `inventory.baseline.json` | doc-lint inventory snapshot（`inventory diff` 用）。fingerprint 抑制とは別系統。 |

### 混同防止（過去の事例 PR #2208 / Issue #2142）

PR #2208 / Issue #2142 では Issue 本文に `inventory.baseline.json` と記述したため、実装者が fingerprint 抑制ファイルと混同した。

Issue 本文に baseline 抑制を記載するときは **必ず `.doc-lint.baseline.json` を正本ファイル名として書く**。
`inventory.baseline.json` は inventory diff 専用であり、fingerprint 抑制には使わない。

### baseline 生成コマンド例

```bash
# Feature spec の baseline を更新する
PYTHONPATH=src uv run python3 -m doc_lint.cli check --scope spec --feature <feature> --update-baseline

# just レシピが使える場合（inventory baseline 更新とは別コマンドであることに注意）
just lint-docs spec <feature>
```

## Blocker / Blocked-by 設定手順

### blocker 検出基準

Issue 起票前に関連 OPEN Issue が「未完了の前提条件」かどうかを以下の基準で判断する:

- 対象機能の実装 PR が未マージ（依存ライブラリ・API の提供元が未完了）
- 依存 spec / 設計書が未確定（本 Issue の実装に必要な仕様が未固定）
- 外部 API / サービスが未公開（本番環境へのデプロイが前提）
- 同一ファイルを変更する別 PR がオープンでコンフリクト可能性がある
- 本 Issue の Acceptance Criteria が別 Issue の完了を条件としている

### 設定方法

```bash
# --blocked-by フラグで blocker Issue 番号を指定する（複数指定可）
python scripts/github_ops/create_issue_txn.py \
  --repo <owner>/<repo> \
  --title "実装: <タイトル>" \
  --blocked-by <blocker_issue_number> \
  --blocked-by <another_blocker_number>

# --dependency フラグは --blocked-by の alias（同等）
python scripts/github_ops/create_issue_txn.py \
  --repo <owner>/<repo> \
  --title "実装: <タイトル>" \
  --dependency <blocker_issue_number>

# 両フラグ併用も許容（list が連結される）
python scripts/github_ops/create_issue_txn.py \
  --repo <owner>/<repo> \
  --title "実装: <タイトル>" \
  --blocked-by 100 \
  --dependency 200
```

### 検証コマンド

```bash
# blocked-by 関係が登録されたことを GraphQL で確認する
# 注意: blockedBy フィールドは GitHub sub-issues ベータ参加リポジトリでのみ利用可。
# 未参加環境では `Field 'blockedBy' doesn't exist` エラーになる。
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

### state/blocked ラベル運用との整合

blocked-by を設定した Issue には `state/blocked` ラベルを付与し、blocker が解除されるまでキューに入れないことを推奨する。blocker が CLOSE されたら `state/blocked` を外して `state/queued` に切り替える。

## Partial-failure Recovery 手順

`create_issue_txn.py` がいずれかのステージで失敗した場合、Issue に partial-failure audit comment が自動投稿される。comment 内の "Recovery hint" セクションを読み、以下の手順で補正する。

### 失敗ステージ別の補正手順

| failed_stage | 補正手順 | idempotent |
|---|---|---|
| `sub-issue-readback` | 1. まず readback で関係の有無を確認する: `gh api repos/<owner>/<repo>/issues/<child>/parent` 2. 未登録が確認できた場合のみ登録を実行する: `gh api repos/<owner>/<repo>/issues/<parent>/sub_issues --method POST -F sub_issue_id=<child_db_id>` | 既存関係 readback で確認後に再実行（重複登録は API がエラーを返す可能性あり） |
| `dependency-readback` / `dependency-register` | 1. まず readback で blockedBy 関係の有無を確認する: `gh api graphql -f query='query{repository(owner:"<owner>",name:"<repo>"){issue(number:<N>){blockedBy(first:10){nodes{number}}}}}` 2. 未登録が確認できた場合のみ登録 mutation を実行する: `gh api graphql -f query='mutation($input:AddBlockedByInput!){addBlockedBy(input:$input){clientMutationId}}' -F 'input[issueId]=<child_node_id>' -F 'input[blockingIssueId]=<blocker_node_id>'` | 既存関係 readback で確認後に再実行（重複登録は API がエラーを返す可能性あり） |
| `label-readback` | `gh issue edit <N> --repo <owner>/<repo> --add-label <labels>` | yes |
| `dedupe-search` / `dedupe-race-detection` | 自動補正不可。同タイトルの open issue を手動で確認し、重複をクローズしてから再実行する | no |
| その他 | `gh issue view <N> --repo <owner>/<repo> --json number,title,labels,state` で状態を確認してから判断する | 依存 |

### node ID の取得方法

```bash
gh api graphql -f query='
query {
  repository(owner:"<owner>", name:"<repo>") {
    issue(number: <N>) {
      id
      databaseId
    }
  }
}'
```

### recovery comment の読み方

`_post_partial_failure_comment` が投稿するコメントの構造:

```
create-issue transaction partial-failure

Issue: #<N>
Failure stage: <stage>
Message: <message>

Requested:
- labels: ...
- parent: ...
- dependencies: ...
Completed steps: ...

Failure context: ...

Recovery hint: <stage固有の補正手順>

Please recover deterministically before re-running the create issue transaction.
```

"Recovery hint:" 以降に補正コマンドと idempotency 情報が記載されている。

## Related

- rule: `.agents/rules/issueops-common-guard.md`
- rule: `.agents/rules/issue-uncertainty-policy.md`
- rule: `.agents/rules/github-ops-workflow.md`
- skill: `.agents/skills/issue-body-authoring/SKILL.md` — 本文編集の shared skill（schema 定義・VC 作成ガイダンス）
- skill: `.agents/skills/review-issue/SKILL.md`
- skill: `.agents/skills/issue-contract-review/SKILL.md`
- skill: `.agents/skills/issueops-operations/SKILL.md`
- template: `templates/github-ops/contract-snapshot.md`
- issue-template: `.github/ISSUE_TEMPLATE/github-ops-implementation.md`
- issue-template: `.github/ISSUE_TEMPLATE/github-ops-parent.md`
- issue-template: `.github/ISSUE_TEMPLATE/github-ops-research.md`
- issue-template: `.github/ISSUE_TEMPLATE/github-ops-human-confirm.md`
