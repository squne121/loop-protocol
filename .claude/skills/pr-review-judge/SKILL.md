---
name: pr-review-judge
description: implementation child issue に紐づく PR を review するときに使う。linked issue の contract と PR 本文 / diff / verify 証跡を照合し、APPROVE または REQUEST_CHANGES を判定し、self-authored PR は `gh pr review --comment` で verdict を記録する。
required_rules:
  - github-ops-workflow
  - issueops-common-guard
  - git-policy
---

# PR Review Judge

linked issue の contract と PR evidence を照合し、merge 可否を判定する skill。
共有運用のガバナンスは `.agents/skills/shared-agent-skills-governance/` に委譲し、この skill は PR review の判断と GitHub surface への記録に集中する。

## 責務分離原則

- **本 skill（pr-review-judge）**: AC 充足判定 + mergeability 判定 + Allowed Paths 検査。以下は**本 skill の責務外**:
  - AC 妥当性検証（「そもそも AC 自体が適切か」の審査）→ `issue-contract-review` の責務
  - project convention 適合（architecture-fit）→ `adversarial-reviewer` の責務
  - セキュリティリスク / 信頼性リスク → `adversarial-reviewer` / `security-reviewer` の責務
- **test-runner**: 検知（mergeable 状態 / hazard test resource limit / baseline failure 等）を `TEST_VERDICT` に統合
- **adversarial-reviewer**: 信頼性リスク観点 + project convention 適合（architecture-fit）観点。PR reviewer（本 skill）の結果を参照しないこと（相互参照禁止）
- **implementation-worker**: 実装 + conflict resolve
- **orchestrator（impl-review-loop）**: state tracking + routing

## Input

- `PR番号` または `PR URL`（必須）
- Linked issue 番号（PR 本文 `Closes #N` から自動取得可）

## Use When

- implementation child issue の PR を review したい
- `Acceptance Criteria -> Evidence` が十分か確認したい
- blocker / non-blocker を分けて返したい
- 「PR ◯◯ レビューして」「review PR」「PR 確認して」などの短文トリガー

## Procedure

- **self-authored PR ガードチェック（冒頭）**:
  - PR author と実行アカウントが同一の場合は `APPROVE` / `REQUEST_CHANGES` を選ばず `--comment` のみを使う。
  - self-authored の場合、`gh pr review --approve` / `gh pr review --request-changes` は使わない。
  - `--comment` で `## Verdict: ...` を記録し、`LINKED_ISSUE_ACTION` / blocker を追記する。

1. linked issue を特定する:
   - PR 本文の `Closes #<N>` を確認する。
   - `Outcome` `Acceptance Criteria` `Allowed Paths` `Verification Commands` `Required Skills` を linked issue から読む。
1.5. **PR mergeability は test-runner の TEST_VERDICT から取得する**:
   - test-runner 出力（machine-readable marker で特定）から `TEST_VERDICT.mergeable` と `TEST_VERDICT.merge_state_status` を読む:
     ```bash
     TEST_VERDICT_BODY=$(gh pr view <PR番号> --json comments --jq '
       [.comments[] | select(.body | contains("<!-- TEST_VERDICT_MACHINE v1 -->"))] | last | .body
     ')
     MERGEABLE=$(echo "$TEST_VERDICT_BODY" | grep "mergeable:" | head -n1 | sed -E 's/.*mergeable:[[:space:]]*//; s/[[:space:]]*$//')
     MERGE_STATE_STATUS=$(echo "$TEST_VERDICT_BODY" | grep "merge_state_status:" | head -n1 | sed -E 's/.*merge_state_status:[[:space:]]*//; s/[[:space:]]*$//')
     ```
   - test-runner コメントが存在しない場合（例: 初回 PR では CI が先行実行されていない）は、最終 merge ガード（下記 Step 4 の `before LOOP_VERDICT` 時点）でのみ `gh pr view --json mergeable` で 1 回確認し、不一致なら test-runner に再実行を促す。
   
   **判定基準**:
   - `mergeable=CONFLICTING` または `merge_state_status=DIRTY` の場合 → **Conflict（CONFLICTING/DIRTY）として REQUEST_CHANGES blocker**
   - `merge_state_status=BLOCKED` の場合 → **Merge blocker（BLOCKED: review/protection 待ち等）として REQUEST_CHANGES blocker**
   - `mergeable=UNKNOWN`（test-runner での 3 回 retry 後も UNKNOWN）の場合 → **Unknown merge state として REQUEST_CHANGES blocker**
   - `mergeable=MERGEABLE` かつ `merge_state_status=CLEAN|UNSTABLE` の場合 → OK（次へ進む）

2. **CI 証跡を確認する**:
   - PR の CI 証跡を以下の手順で確認する。
   - **LOCAL_CI_RESULT / local-ci/just-check head ガード（必須）**:
     - `current_head_sha=$(gh pr view <PR番号> --json headRefOid -q .headRefOid)`
     - `local_ci_status=$(gh api /repos/squne121/KindleAudiobookMakeSystem/commits/$current_head_sha/status --jq '.statuses[] | select(.context=="local-ci/just-check")')`
     - `local_ci_state=$(echo "$local_ci_status" | jq -r .state // empty)`
     - `local_ci_desc=$(echo "$local_ci_status" | jq -r .description // empty)`
     - `local_ci_status` が empty、または `local_ci_desc` が `head_sha=$current_head_sha` を含まない場合は、review 対象 head に対する `local-ci/just-check` 証跡が不足しているため `REQUEST_CHANGES` を追加する。
     - `local_ci_state=success` かつ `head_sha` 一致の場合は CI 証跡あり（pass）として次の判定へ進む。
     - `local_ci_state=failure` かつ `head_sha` 一致の場合は CI 証跡あり（fail）として次の判定へ進む。changed paths が `.agents/skills/**` / `.claude/skills/**` / `.codex/agents/*.toml` / `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` のみに一致する PR では、この failure を直ちに今回差分 blocker と決めつけず、Step 3 / 3.5 の baseline failure 判定へ回す。その他の docs/config path を対象にする場合は、linked issue の Allowed Paths または PR 本文で exact path を明示すること。
     - `local_ci_state` が `pending` / `error` / 空文字のままなら、head-aligned status が安定していないため `REQUEST_CHANGES` を追加する。
   - **GitHub API による CI チェック確認**（優先）:
     ```bash
     # gh pr checks で PR に紐づく CI チェックの最新状態を確認する
     gh pr checks <PR番号>
     ```
     - 出力に `pass` / `success` が含まれるチェックが存在し、`fail` / `failure` / `pending` が存在しない場合 → CI 証跡あり（pass）
     - 出力に `fail` / `failure` が含まれるチェックが存在する場合 → CI 証跡あり（fail）
     - `gh pr checks` で結果が得られない場合（PR が CI に未紐づき等）は、以下でブランチの直近 run を確認する:
       ```bash
       # ブランチの直近 workflow run を確認する
       gh run list --branch <branch名> --limit 5
       # 特定 run の詳細を確認する
       gh run view <run_id>
       ```
   - **CI 証跡ありの判定基準**（以下のいずれかを満たすこと）:
     1. `gh pr checks` の出力で全チェックが `pass` / `success` → CI 証跡あり（pass）として扱う
     2. `gh pr checks` の出力で `fail` / `failure` が存在する → CI 証跡あり（fail）として扱う
     3. または PR Evidence の `Commands Run` に `just check` の数値終了コードを含む行または表 row が記録されている（有効な記録例: `exit 0`・`exit 1`・`| just check docs/rules-target | 0 | ... |`。`→ PASS` / `→ FAIL` などの自然言語表記は有効な証跡として認めない）
     4. または以下の両方を満たす場合（レビュアーが独立して検証すること）:
        a. PR Evidence の `Commands Run` に「`just check` 対象外」と明記されている
        b. かつ `gh pr diff <PR番号> --name-only` の出力が `.agents/skills/**` / `.claude/skills/**` / `.codex/agents/*.toml` / `AGENTS.md` / `CLAUDE.md` / `GEMINI.md`、または linked issue / PR 本文で exact path 指定された docs/config-only path のみを含む
        条件 b が満たされない場合は、条件 a の記述内容にかかわらず免除を適用しない。
   - 上記のいずれも満たさない場合は「CI 証跡なし」と判定する。
     - **CI 証跡なし** → `REQUEST_CHANGES` を返し「PR のローカル CI 検証が未完了です。`ci-runner` サブエージェントを実行してから再レビューしてください」とコメントして終了する。
     - **CI 証跡あり（fail）** → fail を blocker として記録し、Step 3 へ進む（PR evidence review も実施する）。
     - **CI 証跡あり（pass）** → Step 3 へ進む。
   - **（任意・Step 2a）PR ブランチの worktree を作成してローカル参照する**:
     - CI 証跡あり（pass/fail）で Step 3 へ進む場合にのみ実施を検討する。
     - diff だけでは変更の影響範囲が判断しにくい場合に実施する。以下のいずれかに該当するときに有用:
       - 変更ファイルの前後コンテキスト（diff 外の周辺コード）を読む必要がある
       - SerenaMCP でシンボルレベルの影響分析（呼び出し元・参照先の追跡）をしたい
       - ローカルで test / lint / type-check を実行して verify したい
       - 新規ファイルの構造・命名規約をファイルシステム上で直接確認したい
     - 手順:
       1. 既存 worktree が残っている場合は事前に削除する（前回中断時の残存も自動回収）:
          ```bash
          git worktree remove --force tmp/pr-review-<PR番号> 2>/dev/null || true
          ```
       2. PR ブランチを fetch して named ref に保存する（fork PR でも動作、並行レビュー時の FETCH_HEAD 上書きを防ぐ）:
          ```bash
          git fetch origin refs/pull/<PR番号>/head:refs/pr-review/<PR番号>
          git worktree add --detach tmp/pr-review-<PR番号> refs/pr-review/<PR番号>
          ```
       3. worktree 内でファイル参照・SerenaMCP 分析・検証コマンドを実行する。
       4. レビュー完了後に worktree と named ref をクリーンアップする（検証生成物が残っていても強制削除）:
          ```bash
          git worktree remove --force tmp/pr-review-<PR番号>
          git update-ref -d refs/pr-review/<PR番号>
          ```
     - **注意**: worktree 内でのコミット・push は禁止。`--detach` は技術的な書き込みロックではなく、運用上コミット・push を行わない約束として守ること。
     - **省略条件**: 変更ファイル数が 3 件以下かつ追加/削除行数が 50 行以下で、ローカル検証・SerenaMCP 分析が不要と判断した場合はスキップしてよい。

3. **PR evidence をレビューする**（Step 2 で終了しなかった場合は常に実行）:
  - PR 本文の `Acceptance Criteria -> Evidence` を確認する。
  - `Commands Run` / `Changed Paths` / `Risks` / `Rollback` を確認する。
  - `## Linked Issue` の `change_kind` を確認し、`spec_only` を `docs_or_rules_only` lane として扱う。
  - 実 diff を確認し、linked issue の `Allowed Paths` との整合を確認する。
  - AC coverage・Allowed Paths 逸脱・verify 不足・scope 混入を判定し、暫定 blocker リストを作成する。
  - `Commands Run` に placeholder row（例: `` `just check <target>` `` / `` `<verification command>` `` / `Exit Code=<exit-code>` / `<exit-code-or-n/a>`）が残っている場合、その row は証跡として数えず blocker にする。
  - lane ごとの最低確認点:
    - `spec_only` (`docs_or_rules_only`) lane: `Commands Run` の数値 exit code、`Changed Paths`、`Risks`、`Rollback`、`類似 Issue 統合方針` が具体値で埋まっていること。`Knowledge Harvesting` / `Process / Skill / Agent Improvements` / `Long-form Evidence` は一行要約でもよい。
    - `code` lane: runtime change に対応する verify、risk、rollback が具体化されていること。
    - `mixed` lane: code lane の要件に加えて docs/rules 側の統合判断または destination mapping が書かれていること。
   - changed paths が `.agents/skills/**` / `.claude/skills/**` / `.codex/agents/*.toml` / `AGENTS.md` / `CLAUDE.md` / `GEMINI.md`、または linked issue / PR 本文で exact path 指定された docs/config-only path のみから成る PR で `just check` または `local-ci/just-check` が failure の場合、PR 本文に以下の 4 点がそろっているかを必ず確認する。
     1. 既定 `just check` または head-aligned `local-ci/just-check` のコマンド、exit code、失敗要約
     2. 変更パスに対応した targeted check のコマンド、exit code、PASS/FAIL
     3. `git diff --name-only origin/main...HEAD` などで示した changed paths と、failure を出したファイル/テストが今回差分外である根拠
     4. `root main` または base ref で同一 failure が再現すること、または `current_head_sha` に一致する `local-ci/just-check` と TEST_VERDICT が同じ baseline failure を指していることのどちらか
   - 上記 4 点のどれかが欠ける場合、baseline failure 扱いにはせず evidence 不足として blocker に追加する。
   - **（doc-lint / structure チェッカー PR 専用）`cc-sdd` profile の required section 判定が部分一致になっていないか（完全一致を要求する）**
   - **（doc-lint / structure チェッカー PR 専用）checker の公開 API 変更時に `None` / 空配列 / 実値の 3 経路が網羅されているか**
   - **（doc-lint / structure チェッカー PR 専用）親子 list item 境界で verification / requirement を誤って借りていないか**
3a. **live 検証 evidence をチェックする**（live verification が contract で required な PR のみ）:
   1. linked issue / PR contract から live verification の要否と report root / marker / result key を特定する。必要情報が contract にない場合は、PR evidence では補完せず issue contract 側の欠落として扱う。
   2. 実 diff の changed paths を正とし、canonical surface 上の変更に対応する report 群を見つける。
   3. report directory が明示されている場合はその場所を確認し、なければ contract で指定された path pattern / marker を使って report を特定する。
   4. 各 report について `result` が `pass` であることを確認する。
   5. report が存在しない、`result` が `pass` でない、または contract で要求される evidence と一致しない場合は blocker として追加する。

      blocker テキスト例:
      ```
      [live-verification] linked issue / PR contract で live verification が required ですが、
      対応する live 検証レポートが見つかりません（または result が pass ではありません）。
      contract で指定された report root にレポートを追加し、`"result": "pass"` を確認してから再提出してください。
      ```

3.5. **TEST_VERDICT から baseline_only を検出する（baseline failure ケース）**:
   - PR に紐づく最新 test-runner コメントを確認する（machine-readable marker で特定）:
     ```bash
     TEST_VERDICT_BODY=$(gh pr view <PR番号> --json comments --jq '
       [.comments[] | select(.body | contains("<!-- TEST_VERDICT_MACHINE v1 -->"))] | last | .body
     ')
     ```
   - TEST_VERDICT YAML から `baseline_only: true` と head-aligned SHA を検出する:
     ```bash
     echo "$TEST_VERDICT_BODY" | grep -q "baseline_only: true"
     TEST_VERDICT_HEAD_SHA=$(echo "$TEST_VERDICT_BODY" | grep -E "^[[:space:]]*(head_sha|reviewed_head_sha):" | head -n1 | sed -E 's/^[[:space:]]*(head_sha|reviewed_head_sha):[[:space:]]*//; s/[[:space:]]*#.*$//')
     ```
   - **判定基準**:
     - `baseline_only: true` かつ `TEST_VERDICT_HEAD_SHA == current_head_sha` の場合 → **baseline failure として LOOP_VERDICT に `verdict: HALT_BASELINE` を設定**。blocker リストには列挙しない（ハルト判定として別道）
       - ただし reviewer は Step 3 の 4 点 evidence が PR 本文にそろっていることを確認してから `HALT_BASELINE` を採用する。evidence 不足のまま `baseline_only: true` だけで通さない。
       - 想定ケース: changed paths が baseline 例外対象の exact path predicate に一致し、既定 `just check` / `local-ci/just-check` failure が `current_head_sha` と整合しているが、targeted check は PASS している場合。
     - `baseline_only: true` でも `TEST_VERDICT_HEAD_SHA` が欠落または `current_head_sha` と不一致の場合 → stale / non-head-aligned TEST_VERDICT として baseline 判定を無効化し、通常の blocker 扱いに戻す
     - `baseline_only: false` またはフィールド欠落の場合 → 通常の APPROVE / REQUEST_CHANGES 判定へ進む

4. **判定する**:
   - PR mergeability チェック結果に基づいて blocker を追加（Step 1.5 の値を使用）:
     - `mergeable=CONFLICTING` または `merge_state_status=DIRTY` の場合 → **Conflict blocker として REQUEST_CHANGES に分類**
     - `merge_state_status=BLOCKED` の場合 → **Merge blocker（review/protection 待ち等）として REQUEST_CHANGES に分類**
     - `mergeable=UNKNOWN`（test-runner での 3 回 retry 後も UNKNOWN）の場合 → **Unknown state blocker として REQUEST_CHANGES に分類**
   - 上記判定は CI 証跡の有無・AC カバレッジにかかわらず常に適用される（mergeability は実装完了の前提条件）。
   - Step 3 の暫定 blocker リスト + Step 1.5 の mergeability 結果を統合して最終判定を出す。
   - blocker が 0 件なら `APPROVE`、1 件以上なら `REQUEST_CHANGES`。
   - ローカル CI 検証 fail は、`baseline_only: true`、`TEST_VERDICT_HEAD_SHA == current_head_sha`、Step 3 の evidence 4 点がすべてそろった場合を除き blocker とする。
   - **（最終 merge ガード）LOOP_VERDICT コメント投稿直前に 1 回だけ** `gh pr view --json mergeable -q .mergeable` を実行し、test-runner 読み値と不一致を検出した場合は test-runner に再実行を促す（GitHub API の計算遅延回避）。

5. GitHub surface に verdict を残す:
   - **self-authored PR**（PR author と実行アカウントが同一）の場合、APPROVE / REQUEST_CHANGES を問わず `gh pr review --comment` を使う。`--approve` / `--request-changes` は試行しない。
   - **他者の PR** の場合は `gh pr review --approve` / `--request-changes` を使う。
   - canonical surface: `gh pr review --comment --body "..."` （self-authored）または `gh pr review --approve` / `--request-changes`（他者の PR）

5.1. **inline review comment の使い分け（補助 evidence）**

- `LOOP_VERDICT` は Step 5 の PR review コメント内 YAML が `canonical`（正本）です。行単位の action item は inline review comment で補助 evidence として投稿します。
- self-authored PR では `gh pr review --approve` / `--request-changes` を使わず、`gh pr review --comment` のみを使います。
- inline review comment のみを使うときは `gh api` を明示的に使います。`gh pr review` / `gh pr comment` を inline 投稿用途に使うことは避けます。
- 再現可能な `gh api` 投稿例:

  - endpoint: `gh api repos/squne121/KindleAudiobookMakeSystem/pulls/<PR番号>/comments`
    ```bash
    gh api repos/squne121/KindleAudiobookMakeSystem/pulls/<PR番号>/comments \
      -X POST \
      -f body="行単位指摘内容" \
      -f commit_id="<head sha>" \
      -f path="path/to/file.md" \
      -f line=42 \
      -f side="RIGHT"
    ```

  - endpoint: `gh api repos/squne121/KindleAudiobookMakeSystem/pulls/<PR番号>/reviews`
    ```bash
    gh api repos/squne121/KindleAudiobookMakeSystem/pulls/<PR番号>/reviews \
      -X POST \
      -f commit_id="<head sha>" \
      -f event="COMMENT" \
      -F comments='[{"path":"path/to/file.md","position":12,"body":"行単位指摘内容"}]'
    ```

  - REST docs:
    - https://docs.github.com/v3/pulls/comments
    - https://docs.github.com/en/rest/pulls/reviews
    - PR #1872 実例: `discussion_r3179780028`, `discussion_r3179784112`

5.5. **normalized findings の structured comment 記録**:

   > **[CANONICAL SOURCE]** このセクションが FINDING_REF フォーマット仕様の正本です。他のスキルや手順から参照する場合は、本セクションを根拠とします。

   - **機能概要**: PR コメント内に finding_id と scope_classification を記録する structured comment を埋め込み、オーケストラレータが impl-review-loop Step 3.5（LOOP_STATE コメント）で normalized findings との紐付けを自動化できるようにする。
   - **フォーマット仕様**: HTML hidden comment 形式を使う。以下のフォーマットを verdict テンプレートに含める:
     ```html
     <!-- FINDING_REF finding_id=<finding_id> scope=<scope_classification> -->
     ```
     - `finding_id`: adversarial-reviewer が出力した finding の ID。形式: `[title-slug]--[content-hash]`（8文字 SHA-1）
     - `scope`: scope_classification の値。enum: `in_scope` | `out_of_scope` | `wip_downgraded` | `contradiction`
     - 複数の finding がある場合は、複数行のコメントタグを並列記載する（1行 = 1 finding）
   - **配置位置**: verdict コメントの **「判定する」セクション（Step 4）で収集した blocker リスト**直後に、finding ごとに配置する。
   - **生成条件**:
     1. PR が `impl-review-loop` から来ている（linked issue の contract に adversarial-review step が含まれている）場合のみ生成する。
     2. adversarial-reviewer の findings がない場合（verdict: approve または findings: [] の場合）は structured comment を生成しない（出力対象がない）。
     3. 各 finding の `finding_id` と `scope_classification` を adversarial-review 出力から抽出し、対応する structured comment タグを生成する。
   - **適用タイミング**（段階的 ID 確定パターン）:
     - Step 3.5（正規化）: オーケストラレータが adversarial-reviewer の findings に対し、仮の finding_id と scope_classification を採番して記録（LOOP_STATE に markdown 形式で記載）。
     - Step 4（pr-review-judge）: 本フェーズで、Step 3.5 の仮採番値を検証し、最終確定した finding_id と scope_classification を用いて structured comment タグを生成。
   - **実装上の注意**:
     - `finding_id` / `scope_classification` が null または未確定の場合は、placeholder 値を使わず、その finding は skip する（malformed finding と扱う）。
     - structured comment は human-readable ではなく machine-readable であるため、commit message や PR 本文での説明が重複してもよい（clarify の観点から推奨）。

### Structured Comment フォーマット仕様（Section 5.5 補足）

HTML hidden comment 形式を採用した理由:
- GitHub PR コメントは Markdown として解析され、HTML コメント内容は表示されない（UI クリーニング）。
- `grep / regex` での機械的抽出が容易（`<!-- FINDING_REF` パターンで統一）。
- LLM 出力の干渉を最小化できる（Markdown code block 内のパターン記述より、HTML タグの方が LLM の改行・フォーマット変更リスクが低い）。

#### Scope 境界参照（#672 との関連）

Issue #672（HIGH 指摘 Out of Scope 判断ガイド）で定義される「Out of Scope 判断」と本 Issue #795 の「structured comment 規約」は以下のように分離される:
- **#672**: HIGH 指摘が Allowed Paths 外に該当するか**判定する手順**。判定結果は LOOP_STATE の phase / verdict へ反映される。
- **#795**: finding_id と scope_classification を**記録する方法**。記録形式は structured comment（HTML hidden comment）。

両者は独立した関心事であり、本 Issue の structured comment 規約は #672 の判断ガイドに依存しない（#672 の判定結果を記録する channel としても機能するが、#672 の判定ロジック自体は変更しない）。

## Output Contract

**GitHub surface**

- self-authored PR の canonical surface: `gh pr review --comment`（`PullRequestReview` として review 履歴に残る）
- 他者の PR の canonical surface: `gh pr review --approve` / `--request-changes`
- stdout: 実行ログと要約のみ。verdict の正本にはしない。

self-authored PR の場合（APPROVE / REQUEST_CHANGES 共通）:

    gh pr review <PR番号> --comment --body "## Verdict: APPROVE ..."
    gh pr review <PR番号> --comment --body "## Verdict: REQUEST_CHANGES ..."

他者の PR の場合:

    gh pr review <PR番号> --approve --body "## Verdict: APPROVE ..."
    gh pr review <PR番号> --request-changes --body "## Verdict: REQUEST_CHANGES ..."

**verdict テンプレート（--body に渡す内容）:**

    ## Verdict: [APPROVE / REQUEST_CHANGES]

    ### Baseline Failure（既存問題 — 今回差分と無関係）
    <!-- baseline failure: main ブランチで既存する問題・技術的負債。実装者が今回対応不要のもの -->
    - なし / <item>

    ### 今回差分 Blocker（今回の変更に起因する blocker）
    <!-- diff blocker: この PR でマージをブロックする問題。実装者が今すぐ修正すべき対象 -->
    - なし / <item>

    ### Non-blockers
    - なし / <item>

    ### Mergeability
    - mergeable=<MERGEABLE|CONFLICTING|UNKNOWN>, mergeStateStatus=<CLEAN|UNSTABLE|DIRTY|BLOCKED|UNKNOWN>
    - Classification: <none | conflict (CONFLICTING/DIRTY) | blocked (BLOCKED) | unknown (UNKNOWN after retry)>

    ### Evidence Check
    - AC coverage:
    - Allowed Paths:
    - CI Verification: （Step 2: gh pr checks の結果、または gh run list の結果、または head-aligned `local-ci/just-check` / `just check` の数値終了コード。免除条件4適用時は「免除: 変更パスが exact path predicate に一致し、`just check` 対象外であることを確認」と記録）
    - AC Verification: （Step 3: linked issue の Verification Commands 実行結果）
    - Changed Paths:
    - Baseline Classification Evidence: （既定 `just check` / `local-ci/just-check`、targeted check、failure 要約、今回差分外である根拠、`root main` / base ref 再現または `current_head_sha` と一致する TEST_VERDICT + commit status）
    - Live Verification:

    ### Normalized Findings Reference（adversarial-review の findings が存在する場合）
    <!-- FINDING_REF finding_id=<slug>--<hash> scope=in_scope -->
    <!-- FINDING_REF finding_id=<slug>--<hash> scope=out_of_scope -->
    <!-- FINDING_REF finding_id=<slug>--<hash> scope=wip_downgraded -->

## Stop Conditions

- linked issue が特定できない → 判定せず「`Closes #N` を PR 本文に追加してください」と返す
- PR 本文に `Closes #N` がない → 「linked issue を明示してください」と返す
- `Commands Run` / Evidence が空欄 → `REQUEST_CHANGES`（雰囲気で通さない）
- linked issue / PR contract で live verification が required なのに対応する report が存在しない → `REQUEST_CHANGES`
- live 検証レポートの `result` フィールドが `pass` でない → `REQUEST_CHANGES`
- GitHub Free private repo では `local-ci/just-check` を required status check として強制しない。Pro/Team で必要な場合のみ branch protection に手動設定していることを明記し、未設定なら本 Issue では optional manual setup と扱う。

## Guardrails

- linked issue が不明な PR は判定しない。
- Issue contract にない完了条件を追加しない。
- Evidence 不足を「雰囲気」で通さない。
- self-authored PR では `gh pr review --approve` / `--request-changes` を使わない。verdict は `gh pr review --comment` で記録する。
- `RESULT_PASS_BLOCKER_GUARD: live verification report の result が pass でない PR は blocker として扱う`

## LOOP_VERDICT スキーマ

**verdict コメント内の LOOP_VERDICT コードブロック:**

verdict コメント（`gh pr review --comment` または `gh pr review --approve/--request-changes` で記録される）に、以下の YAML スキーマで LOOP_VERDICT 情報を含める。これは impl-review-loop による自動判定に使用される。

    ## LOOP_VERDICT
    ```yaml
    verdict: APPROVE
    blockers: []
    mergeable: MERGEABLE        # MERGEABLE | CONFLICTING | UNKNOWN
    mergeStateStatus: CLEAN     # CLEAN | UNSTABLE | DIRTY | BLOCKED | UNKNOWN
    reviewed_head_sha: <SHA>    # pr-reviewer がレビューした PR head SHA（オーケストラレータから渡された値）
    ```

- `verdict`: 最終的な判定（APPROVE | REQUEST_CHANGES | HALT_BASELINE）
  - `APPROVE`: AC 充足、blocker なし。merge 可能
  - `REQUEST_CHANGES`: blocker あり。実装者の修正待ち
  - `HALT_BASELINE`: baseline-only failure 検出。orchestrator が halted_baseline phase へ遷移
- `blockers`: blocker リスト。0 件の場合は `[]` 、複数の場合は `["blocker1", "blocker2"]` の形式
- `mergeable`: 最後に確認した mergeable 値
- `mergeStateStatus`: 最後に確認した mergeStateStatus 値
- `reviewed_head_sha`: **【必須】** pr-reviewer がレビューした PR head SHA。オーケストラレータから渡された値をこのフィールドに転記する。このフィールドは **YAML ブロック内に必ず記載し、ブロック外への記載は禁止**。

**【重要な制約】**

1. **`reviewed_head_sha` は必ず YAML ブロック内に記載する**:
   - YAML ブロック外（```の外側）への記載は禁止する。
   - Step 5 の自動パーサは LOOP_VERDICT コメント全体から `reviewed_head_sha:` を行単位で抽出する（YAML ブロック内外は区別しない）。そのため YAML ブロック外に記載した場合でも常に読み取られるが、YAML 正本フィールドの位置保証と可読性のため、必ずブロック内に記載すること。
   - コメント本文全体で `reviewed_head_sha:` 行は 1 つだけにすること（重複記載禁止）。複数記載した場合、最初にマッチした行が採用されるため（Step 5 パーサー実装: `.agents/skills/impl-review-loop/steps/step-5-mergeability-handling.md` の `grep -E "^[[:space:]]*reviewed_head_sha:" | head -n1`）、説明文やリストに `reviewed_head_sha:` を含めると意図しない値が抽出される場合がある。
2. **実投稿時はバックスラッシュ無しの ``` を直接書くこと**: verdict 本文内のコードフェンス（` ``` `）は `\` でエスケープしないこと。`gh pr review --body` の heredoc 内でも `\`\`\`yaml` ではなく ``` ` を直接書く。エスケープした場合、GitHub PR コメントに `\`\`\`yaml` が literal で表示され、自動パーサが YAML ブロックを抽出できない。

注: impl-review-loop スキルでは、LOOP_VERDICT の自動読み取り時に reviews と comments を時系列ソートして最新コメントを確実に参照しています。詳細は impl-review-loop SKILL の P-2 / Step 5 節を参照してください。
## Validation Commands

- `gh pr review <PR番号> --comment --body "## Verdict: APPROVE ..."` で self-authored PR の verdict を記録する。
- `gh pr review <PR番号> --comment --body "## Verdict: REQUEST_CHANGES ..."` で self-authored PR の verdict を記録する。
- `gh pr review <PR番号> --approve --body "## Verdict: APPROVE ..."` で他者の PR を承認する。
- `gh pr review <PR番号> --request-changes --body "## Verdict: REQUEST_CHANGES ..."` で他者の PR に変更を要求する。

## Handoff Prompt Draft

- `linked issue` `AC coverage` `Allowed Paths` `Verification` `Changed Paths` を次のエージェントが追跡できる形で GitHub surface に残す。

## Related

- rule: `.agents/rules/issueops-common-guard.md`
- skill: `.agents/skills/shared-agent-skills-governance/SKILL.md`
- skill: `.agents/skills/implement-issue/SKILL.md`
- reference: `.agents/skills/pr-review-judge/references/best-practices.md`
- reference: `.agents/skills/pr-review-judge/references/review-output-contract.md`
- template: `.github/PULL_REQUEST_TEMPLATE.md`
- template: `templates/github-ops/pr-evidence.md`
