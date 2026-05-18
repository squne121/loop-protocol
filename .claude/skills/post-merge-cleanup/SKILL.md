---
name: post-merge-cleanup
description: PR マージ後のローカル cleanup と Git 整理が必要なときに使う。未コミット確認・main整合・worktree/branch整理・parent issue確認・follow-up起票を行う。
---

# Post Merge Cleanup

PR マージ後のローカル環境 cleanup と Git 状態整理を行うスキル。

## Use When

- PR マージ後のローカル cleanup と Git 整理が必要なとき
- 「クリーンアップして」「マージ後の整理して」「cleanup」「post merge」などの短文トリガー
- worktree / branch の安全な整理が必要なとき
- parent issue のクローズ条件を確認したいとき
- superseded PR を canonical PR へ収束させ、destination mapping を GitHub surface に残したいとき

## Delegation（SubAgent 委譲）

main thread が `/post-merge-cleanup` または `post-merge-cleanup` スキルを起動する際は、以下の手順で `post-merge-cleanup-worker` SubAgent へ委譲する。

### 委譲手順

1. main thread が Agent tool で `post-merge-cleanup-worker` SubAgent を起動する:

   ```
   # Agent tool への入力例
   SubAgent: post-merge-cleanup-worker
   入力:
     merged_pr_number: <マージした PR 番号>（ステップ 5・6 実行時は必須。省略時はステップ 5・6 を skip して unresolved_cleanup_items に記録）
     linked_issue_number: <linked issue 番号（任意）>
     context: "<cleanup に必要な追加コンテキスト（任意）>"
   ```

2. SubAgent は `POST_MERGE_CLEANUP_REPORT_V1` YAML を返却する
3. main thread は返却された YAML を受け取り、以下を実行する:
   - `human_review_required: true` の場合: `unresolved_cleanup_items` を確認し、人間に判断を求める。
     - `git pull` CONFLICT 由来の場合: main thread は SubAgent をリトライせず、人間に「`git merge --abort` または `git rebase --abort` で操作を取り消してから手動で解消する」よう指示する。解消後に再度 `/post-merge-cleanup` を起動する。
     - `merged_pr_number not provided` 由来の場合: main thread が PR 番号を確定して再委譲する。
     - その他は `unresolved_cleanup_items` の理由ごとに main thread が個別判断する。
   - `follow_up_candidates` に候補がある場合: routing 種別（`create-issue` / `issue-author`）を選択し、follow-up 起票を実行する（起票実行は main thread の責務）
   - `superseded_prs` に候補がある場合: main thread が `gh pr close` / `gh pr comment` で close / comment を実行する（SubAgent は候補列挙のみ）
   - `parent_issue_status` に推奨アクションがある場合: main thread が `gh issue close` で parent issue をクローズする（SubAgent は条件確認のみ）
   - `stash_restored: false` または `stash_entry_ref` が非 null の場合: `stash_entry_ref` を確認し、人間または main thread が stash の扱いを判断する

### 責務分界

| 責務 | 担当 |
|---|---|
| git/gh 出力分類・cleanup 実行 | `post-merge-cleanup-worker` SubAgent |
| CONFLICT 検出・人間確認要求の即時報告 | `post-merge-cleanup-worker` SubAgent（fail-close） |
| follow-up Issue 起票 | main thread（`create-issue` / `issue-author` に委譲） |
| routing 種別選択（issue-author vs create-issue） | main thread |
| parent issue クローズ実行 | main thread |
| superseded PR close/comment 実行 | main thread |
| 人間判断が必要な事象の最終判断 | 人間 |

SubAgent が返却した `POST_MERGE_CLEANUP_REPORT_V1` の `follow_up_candidates` フィールドには候補が列挙されているが、起票実行と routing 種別の最終選択は main thread が行う。

## Procedure

**実行方針**: 未コミット変更・未追跡ファイルを検出した場合でも、安全に実行できるステップから先行実行し、不明点のみステップ6のレポートにまとめて報告してください。即座に停止して確認を待つのではなく、main sync・リモート削除済みブランチ/worktree の削除・parent issue 確認を進めてください。

**入力面の確認**: cleanup wording / destination mapping / routing precedence を修正するときは、少なくとも `PR #1990`、`PR #1993`、`issue #1960` comment surface、PR 本文の `類似 Issue 統合方針` / `Follow-ups Intentionally Deferred` / `Knowledge Harvesting`、cleanup destination mapping comment を見て、recent merged PR の recurring pattern を確認してください。routing precedence を扱う場合は `#1633 / PR #1637` の `issue-author` precedent、`#1856` の `create-issue` canonical entrypoint、`#1909 / PR #1919` の repo reality guard も参照してください。

1. 未コミット変更と未追跡ファイルを分類する:
   ```bash
   git status
   ```
   - **削除対象（自動削除してよい）**:
     - リモート削除済みブランチに対応する worktree（ステップ3で削除）
     - リモート削除済みの branch（ステップ3で削除）
   - **報告対象（削除せず、ステップ6で報告）**:
     - staged / unstaged 変更: 退避漏れの可能性あり。削除せず理由とともに報告する。
     - 未追跡ファイル（untracked files）: checkpoint や中途成果物などの既知 artifact の可能性あり。削除せず、パスと内容の推定を報告する。
     - 意図不明なファイル・ディレクトリ: 削除可否が不明な場合は削除せず、詳細を報告する。
   - **この時点では削除しない**。分類結果をステップ6で報告してください。
2. main を origin/main に整合させる（先行実行）:
   ```bash
   # staged 変更がある場合は先に退避してから checkout する
   STAGED=$(git diff --cached --name-only)
   if [ -n "$STAGED" ]; then
     echo "[INFO] staged 変更を一時退避します（git stash）"
     git stash
   fi
   git checkout main
   git pull origin main
   ```
   - **staged 変更がある場合**: `git checkout main` 前に `git stash` で退避する。`git checkout main` を staged 変更ありで実行すると、staged 変更が main ブランチに carry over されるリスクがあるため（Git の既知動作）。
   - CONFLICT が発生した場合は停止し、人間に確認する。
   - 成功した場合はステップ3へ進んでください（**ただし untracked files のみの場合に限り**、staged/unstaged 変更がある場合は stash 後に進む）。
3. worktree / branch を整理する（先行実行）:
   - マージ済み・リモート削除済みのブランチを確認する:
     ```bash
     git branch -vv | grep ': gone]'
     ```
   - 各ブランチについて「消してよいと確定できる」条件を満たすか判定する:
     - リモートが削除済み（`gone`）
     - ローカルに staged/unstaged 変更がない
     - linked Issue がクローズ済み
   - **branch 削除**（未追跡ファイルがあってもローカルファイルには影響しない）:
     ```bash
     git branch -d <branch-name>
     ```
   - **worktree 削除前に確認**（worktree は丸ごと削除されるため staged 変更・未追跡ファイルも消える）:
     - worktree の staged 変更と未追跡ファイルを確認する:
       ```bash
       STAGED=$(git -C "<worktree_path>" diff --cached --name-only 2>/dev/null)
       UNTRACKED=$(git -C "<worktree_path>" status --short 2>/dev/null | grep -E '^\?\?' || true)
       if [ -n "$STAGED" ]; then
         echo "staged 変更あり: worktree を削除しない（ステップ6で報告）"
       elif [ -n "$UNTRACKED" ]; then
         echo "未追跡ファイルあり: worktree を削除しない（ステップ6で報告）"
       else
         git worktree remove <path>
       fi
       ```
     - staged 変更・未追跡ファイルがない場合のみ削除する。
   - 確定できないものは削除せず、ステップ6で報告する。
4. parent issue のクローズ条件を確認する（確認部分のみ実行）:
   - `gh api repos/{owner}/{repo}/issues/{child_number} --jq '.parent_issue_url // empty'` で parent_issue_url を確認する。
   - 全 sub-issue の状態を収集し、判定結果と推奨アクションを `parent_issue_status` に構造化記録する。
   - **parent issue の close 操作（`gh issue close`）は実行しない**。条件確認と推奨アクションの記録のみを行い、実際の close は main thread が担当する。
   - 未完了 sub-issue がある場合はその番号と推奨アクションを `parent_issue_status` に記録する。
5. canonical PR と superseded PR を GitHub 上で収束させる（候補列挙のみ）:
   - `merged_pr_number` 未提供時はこのステップを skip して `unresolved_cleanup_items` に `merged_pr_number not provided, steps 5/6 skipped` を記録する。
   - merged PR / linked issue / PR comments から `canonical_pr` と `superseded_prs` を抽出する:
     ```bash
     MERGED_PR_JSON=$(gh pr view <PR番号> --json number,url,body,comments,closingIssuesReferences)
     LINKED_ISSUES=$(printf '%s' "$MERGED_PR_JSON" | jq -r '.closingIssuesReferences[].number')
     ```
   - linked issue ごとに、同一 issue を本文で参照する PR を取得する:
     ```bash
     gh pr list --state all --search "\"#<issue番号>\" in:body" \
       --json number,title,state,url,headRefName,updatedAt
     ```
   - canonical PR の判定優先順位:
     - merged 済みで現在の cleanup 対象 PR
     - LOOP_STATE / issue comment に `canonical_pr_url` が明記された PR
     - superseded comment に `Superseded by #<PR番号>` が書かれている場合の参照先 PR
   - canonical 以外で `OPEN` の PR は superseded 候補として `superseded_prs` に列挙する。**close / comment 実行は行わない**（main thread の責務）。main thread が以下のコマンドを実行する:
     ```bash
     # main thread が実行する（SubAgent は実行しない）
     gh pr comment <superseded_pr_number> --body "Superseded by <canonical_pr_url>.\n\nIssue #<issue番号> の canonical PR は <canonical_pr_url> です。post-merge-cleanup によりこの PR を close します。"
     gh pr close <superseded_pr_number>
     ```
   - linked issue にも canonical / superseded の対応を comment として残し、source issue から次の人間作業先が追える状態にする（comment 実行は main thread が担当）。
   - **new comment target は OPEN issue のみ**。comment を追加する前に destination issue の state を確認し、`OPEN` の issue だけを新規 comment target にする。
   - **closed issue は reference-only**。`CLOSED` の issue は destination / precedent / superseded / existing boundary として参照してよいが、新規 comment target にしてはならない。必要な mapping は open issue または canonical PR 上へ残す。
   - `CLOSED` の superseded PR には再 close せず、destination mapping コメントの有無と `canonical_pr_url` の一致を確認する。不一致なら `superseded_prs` に `action: correction_comment_needed` として記録し、main thread が以下を実行する:
     ```bash
     # main thread が実行する（SubAgent は実行しない）
     gh pr comment <superseded_pr_number> --body "Correction: canonical PR is <canonical_pr_url>.\n\nEarlier superseded mapping comment is superseded by this correction."
     ```
   - attached worktree / branch drift residue を見つけた場合は、close / 削除より先に canonical evidence bundle を回収する:
     ```bash
     git reflog --date=iso --all
     git worktree list
     git status --short --branch
     git rev-parse HEAD
     git branch --show-current
     ```
   - `unexpected init commit ancestry`, `outside.txt`, `wip/demo/module.py` のような drift signature を comment に残し、current head / expected head / branch と `detached verify head switch decision` を canonical-head handoff として issue / PR に記録する。
   - この evidence capture は workflow handoff のためのものであり、profile routing の runtime fix を `#1948 / PR #1977` から奪わず、`pre-push` helper cancel/ignore も `#1978` の既存 destination に留める。
6. Follow-ups Intentionally Deferred と Knowledge Harvesting から Issue を自動起票する:
   - PR 本文に `## Remaining Parent Gaps` がある場合は、`## Follow-ups Intentionally Deferred` より先に読み、parent issue に返す gap と新規 follow-up 候補を区別する。`Remaining Parent Gaps` が `なし` でないのに open destination を特定できない場合は `no-open-destination` として fail-close する。
   - parent issue に返すだけで十分な残件は destination mapping comment で parent へ戻し、child issue を追加しない。新しい `1 Issue = 1 PR` 作業単位が必要な残件だけを `sub-issues` または `issue dependencies` の候補として扱う。
   - マージした PR の body から「Follow-ups Intentionally Deferred」セクションを抽出する:
     ```bash
     MERGED_PR_BODY=$(gh pr view <PR番号> --json body -q .body)
     # "Follow-ups Intentionally Deferred" 以降の内容を抽出
     ```
   - follow-up 起票前に repo reality check を行う:
     - 対象 follow-up が参照する tool CLI / output contract / artifact path の矛盾（`analysis_output/` と `reports/` 混在など）がないか確認する。
     - `--help` / `ls ... || echo` のみで真偽を判断しない。exit code と実体の artifact 生成先を一致させる。
     - repo reality 不一致を検知した場合は、research issue を ready 扱いせず blocked-by の implementation child issue を先に切る。
     - blocked-by の実施後、再現手順と停止理由を採用した `follow-up issue flow` の委譲入力に含める。
   - follow-up issue flow と destination mapping comment の routing は次の decision table に従う:

     | 条件 | follow-up issue flow | destination mapping comment | report label |
     |---|---|---|---|
     | source issue / source PR / orchestrator が `issue-author` を明示 | `issue-author routing` を優先 | open issue があればその issue に comment、なければ canonical PR に `issue-author routing` と `no-open-destination` を記録 | `issue-author routing` |
     | `issue-author` 明示なし | `create-issue routing` を既定に使う | open issue があればその issue に comment、なければ canonical PR に `create-issue routing` と `no-open-destination` を記録 | `create-issue routing` |

   - `issue-author` が明示された場合は `issue-author routing` を優先し、明示がない場合は `create-issue routing` を既定とする。`post-merge-cleanup` 本体は独自の GitHub mutation path を持たず、follow-up issue flow は `issue-author` または `create-issue` のどちらかへ委譲する。
   - open の canonical destination が見つからない場合は、closed issue に comment せず `no-open-destination` として fail-close で報告する。必要なら `follow-up issue flow` を使って新しい open destination を作るが、closed issue への新規 comment では代替しない。
   - 各 Follow-up 項目は採用した `follow-up issue flow` に委譲して自動起票する（人間確認なし）。`issue-author` 明示時は `issue-author routing`、それ以外は `create-issue routing` を使う。**ワークフロー不具合を扱う follow-up の場合は、採用した routing で委譲する際に issue-body-authoring ガイドラインの「ワークフロー不具合検出時の修正方針起案ガイダンス」を参照し、決定論的修正優先で Outcome を起案するよう入力コンテキストに明記する**:
     ```bash
     # 例: "Fix authorization in batch API" を follow-up 候補として follow-up issue flow へ委譲
     # delegated_by=post-merge-cleanup, linked_context=$MERGED_PR_URL を付与して issue-author または create-issue を呼び出す
     # ワークフロー不具合関連の follow-up の場合は、決定論的修正ガイダンスの参照を入力に含める
     ```
   - PR body の「Knowledge Harvesting」テーブルに再利用可能な知見がある場合も、各行を採用した `follow-up issue flow` に委譲して自動起票する:
     ```bash
     # 例: テーブルの "ページ 0 は有効値" を knowledge-harvest 候補として follow-up issue flow へ委譲
     # delegated_by=post-merge-cleanup, linked_context=$MERGED_PR_URL を付与して issue-author または create-issue を呼び出す
     ```
   - **フォローアップ Issue 起票の自動化**: 人間確認ステップは不要。post-merge-cleanup の責務は PR body の Follow-ups と Knowledge Harvesting から候補を抽出し、`issue-author` 明示時は `issue-author routing`、それ以外は `create-issue routing` に委譲すること。起票した Issue URL と採用 routing を PR コメント（または open issue comment）に記録する。
   - #1900 / #1908 再発防止の起票分離例:
     - `analysis_output/` が期待で `reports/...` が実体のような混線を検知した場合、まず implementation child issue（例: `blocked-by:`）を切る。
     - `--help` や `ls` のみ、または `not yet created` 文字列だけを根拠に research follow-up を ready にしない。
7. 最終レポートにまとめて報告する:
   - 実施した cleanup の要約（ステップ2・3・4・5で実行した内容と結果）
   - main と origin/main の整合結果
   - 削除した branch / worktree の一覧
   - close / mapping した superseded PR の一覧
   - drift residue を引き継いだ場合の canonical-head handoff（evidence bundle, current head / expected head / branch, detached verify head switch decision）
   - **不明点・報告対象（削除しなかった理由付き）**:
     - staged/unstaged 変更の内容と推定理由
     - 未追跡ファイル（untracked files）のパスと推定用途
     - 意図不明なファイル・ディレクトリの詳細
     - リモート削除済みだが削除できなかったブランチ/worktree の理由
     - canonical PR を一意に決められなかった superseded PR の理由
   - Follow-up Issue 案（あれば）
   - 現在のローカル Git 状態の要約
8. ステップ2で git stash を実行した場合の staged 変更復帰手順:
   ```bash
   git stash list
   ```
   - **破棄してよい場合**（退避漏れではなく不要なもの）: `git stash drop` で削除する
   - **復帰が必要な場合**（別ブランチに移動させたい）: `git stash pop` または `git stash apply` で復帰する
     - `git stash pop` で conflict が発生した場合は人間に確認する
     - `git stash apply` を使う場合は手動で `git stash drop` を実行して退避を削除する

## Output

- **実施した cleanup**: コマンドと結果の一覧
- **main と origin/main の整合結果**: 同期済み / 差分あり（内容）
- **削除した branch / worktree**: 削除対象と削除結果
- **close / mapping した superseded PR**: PR 番号、canonical PR、実施した comment
- **コメント追記した issue**: issue 番号、comment 理由、採用した routing（`issue-author routing` / `create-issue routing`）
- **参照のみの destination issue**: issue 番号、reference-only とした理由
- **follow-up として委譲した issue**: 起票先 issue、採用 routing、linked context
- **統合しなかった類似 issue**: issue 番号、非統合理由、必要なら推奨後続アクション
- **削除しなかった branch / worktree**: 理由付き
- **残っているローカル差分の有無**: あり（内容）/ なし
- **Follow-up Issue 案**: 必要な場合のみ
- **routing / destination 判定**: `issue-author routing` / `create-issue routing` / `no-open-destination` のどれを採用したか
- **現在のローカル Git 状態の要約**: `git status` 出力の要約

## Guardrails

- **staged 変更の carry-over リスク**: `git checkout` を staged 変更ありで実行すると、staged 変更がチェックアウト先ブランチに carry over される（Git の既知動作）。ステップ2で `git checkout main` を実行する前に必ず `git stash` で staged 変更を退避する。
- 破壊的操作（`git branch -d`, `git worktree remove`）の前に必ず未コミット変更（staged/unstaged）の有無を確認する。
- **削除判定の厳密さレベル**（リモート削除済みブランチ/worktree の対象）:
  - **branch 削除の条件** (`git branch -d`は untracked files を消さない):
    - staged/unstaged 変更がない → 削除してよい（実装完了・退避完了の状態）
    - 未追跡ファイルのみ → 削除してよい（ローカルファイルは消えず、artifact は残る）
  - **worktree 削除の条件** (`git worktree remove`は worktree ディレクトリを丸ごと削除するため untracked files も消える):
    - staged/unstaged 変更がない → 削除可（ステップ3で確認）
    - 未追跡ファイルがない → 削除可（ステップ3の確認ステップで検証）
    - 未追跡ファイルがある → 削除しない（ステップ6で報告。人間が削除可否を判断）
- 未追跡ファイル（untracked files）は以下のいずれでも削除しない。パスと推定用途を記録してステップ6で報告する：
  - 削除可否が不明な場合（用途が推定できない）
  - 用途が確認されていない場合（実装者が削除意図を表明していない）
- `git branch -D`（force delete）は人間の明示的な指示がある場合のみ実行する。
- Issue / Task / Follow-up の取りこぼしを確認してから cleanup を完了とする。
- superseded PR を close する前に、canonical PR URL と linked issue への destination mapping comment を必ず残す。
- destination mapping comment で closed issue を新規 comment target にしない。open destination がなければ `no-open-destination` を報告し、必要なら `follow-up issue flow` に委譲する。
- 実行方針「不明点のみ最終レポートにまとめて報告」とは、ステップ2・3・4で判定がついたもの（削除対象など）は即座に実行し、判定が未確定なもの（用途不明なファイルなど）のみレポートに含めること。確認待ちで全ステップが停止することはない。

## Related

- rule: `.agents/rules/issueops-common-guard.md`
- rule: `.agents/rules/git-policy.md`
- skill: `.agents/skills/issueops-operations/SKILL.md`（Issue Completion ステップ6）
- skill: `.agents/skills/create-issue/SKILL.md`（Follow-up Issue 生成）
- precedent: `#1633 / PR #1637`（`issue-author` routing）
- precedent: `#1856`（`create-issue` canonical entrypoint）
- precedent: `#1909 / PR #1919`（repo reality guard）
- related implementation issue: `#1994`, integrated concern: `#1998`
