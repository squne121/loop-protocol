### Step 0: Rules Loading Preflight

orchestrator は SubAgent 委譲前に以下の preflight を実行し、rules を context に inline 注入する。冪等マーカー `<active_rules ...>` により重複読込を防止する。

---

#### 設計方針: Selective inline by role manifest + Cache marker 拡張（Alt 3 + Alt 4 Hybrid）

**採択経緯（Issue #2276 設計判定）**

本 Step 0 は SubAgent 委譲時の prompt 長膨張を抑えるため、以下の 4 案を比較検討した末、Alt 3 + Alt 4 の Hybrid を採択した。

| 案 | 概要 | 判定 |
|---|---|---|
| **Alt 1**: rule-id 列挙 / controlled-pull | SubAgent が必要な rule-id だけを自律 Read する | **却下**: 既存 Step 0 設計意図「SubAgent 自律 Read 禁止」と矛盾し、決定論性が下がる |
| **Alt 2**: RuleFetch tool 化 | Rule Read を専用 tool に分離する | **却下**: 新 tool 登録が必要で本 Issue scope 外 |
| **Alt 3**: Selective inline by role manifest | 各 SubAgent role が必要とする rule subset のみ inline 注入し、baseline prompt size を 40-60% 削減 | **採択（主）** |
| **Alt 4**: Cache marker 拡張 | 同 iteration 内の複数 SubAgent 委譲で `<active_rules>` マーカーを orchestrator 状態として保持し、重複 Read を 1 回に集約 | **採択（補助）** |

**選定理由**: Alt 3 が prompt size 削減の主因（role 不要 rules を除外）。Alt 4 は orchestrator 側の iteration 内重複 Read を防ぐ運用補強。Alt 1 は SubAgent 自律 Read 禁止原則と矛盾するため不採択。Alt 2 は scope 外。

---

#### Role-Rule Manifest（正本テーブル）

orchestrator が SubAgent 委譲時に inline 注入すべき rule subset を role 別に定義する。`impl-review-loop` SKILL.md frontmatter `required_rules:` に列挙された 9 rule のうち、各 SubAgent が実際に判断材料として必要とするものだけを抽出する。

| SubAgent role | inline 注入する rule-id | 除外する rule-id（理由） |
|---|---|---|
| `implementation-worker` | `github-ops-workflow`, `issueops-common-guard`, `git-policy`, `file-edit-protocol`, `skill-sync-policy`, `issue-uncertainty-policy` | `skill-rule-boundary`（skill author 向け、runtime 不要）, `orchestrator-skill-policy`（orchestrator 自身向け）, `subagent-design-policy`（SubAgent 設計者向け、runtime 不要） |
| `pr-reviewer` | `github-ops-workflow`, `issueops-common-guard`, `issue-uncertainty-policy` | `git-policy`（編集なし）, `file-edit-protocol`（編集なし）, `skill-sync-policy`（編集なし）, `skill-rule-boundary`, `orchestrator-skill-policy`, `subagent-design-policy` |
| `adversarial-reviewer` | `github-ops-workflow`, `issueops-common-guard`, `issue-uncertainty-policy` | pr-reviewer と同じ |
| `test-runner` | `issueops-common-guard`, `issue-uncertainty-policy` | `github-ops-workflow`（PR 操作なし）, `git-policy`（編集なし）, `file-edit-protocol`（編集なし）, `skill-sync-policy`, `skill-rule-boundary`, `orchestrator-skill-policy`, `subagent-design-policy` |
| `spec-document-reviewer` | `issueops-common-guard`, `issue-uncertainty-policy` | test-runner と同じ |
| `codebase-investigator` / `web-researcher` | `issueops-common-guard`, `issue-uncertainty-policy` | test-runner と同じ |

**運用ルール**:
- orchestrator は SubAgent 委譲時に上表に従って rule subset を抽出し、`<active_rules id1, id2, ...>` マーカーには **その SubAgent に注入した subset のみ**を列挙する（マーカー regex は不変）。
- LOOP_STATE の `active_rules` は **orchestrator 側 superset**（解決済み全 rule の和集合）を引き続き記録する。SubAgent prompt の `<active_rules>` マーカーは **その委譲時点の subset** を記録する。両者を区別すること（混同注意: 同じマーカー形式だが層が異なる）。
- 上表に追加すべき role や、role の rule subset を変更する場合は、本ファイル（`preparation.md`）の表を更新する。SubAgent 自律 Read は引き続き禁止。
- **manifest 未登録 role の fallback**: orchestrator が委譲対象として manifest テーブルに存在しない SubAgent role（typo を含む）を検出した場合は、**fail-close で superset 全量を inline 注入**（従来挙動と同等）する。同時に LOOP_STATE に `manifest_fallback: { role: "<role>", reason: "unregistered_role" }` を記録する。これにより rule 欠落による SubAgent 動作不定を防ぐ（安全側デフォルト）。後日 manifest テーブルに当該 role を追加するか、typo を修正することで fallback 経路から離脱する。

---

**実行手順:**

1. `.agents/rules/index.md` を読み、起動対象 SubAgent（`implementation-worker` / `pr-reviewer` / `adversarial-reviewer` / `test-runner` 等）と参照 skill（`implement-issue` / `pr-review-judge` / `adversarial-review`）の frontmatter `required_rules:` を確認する。

2. 各 `required_rules:` に列挙された rule-id を収集し、重複を除いた「現在の **superset**（active rule set の全体）」を確定する。この superset が LOOP_STATE に記録する正本であり、各 SubAgent に注入する subset の源泉となる。

2.5. **整合検証 (fail-close)**: 手順 2 で確定した `required_rules` superset を、本ファイル上部の Role-Rule Manifest テーブル全 row の rule-id 集合 (union) と突き合わせる。
   - **manifest_union ⊆ superset** であることを確認する（manifest が superset に存在しない未知の rule-id を参照していないこと）。
   - manifest 全 row union に superset 外の rule-id が含まれている場合 → fail-close。LOOP_STATE に `manifest_drift_detected: true` を記録し、main thread に報告して停止する。
   - superset に存在し manifest 全 row union に存在しない rule-id がある場合 → manifest が stale の可能性があるため、LOOP_STATE に `manifest_drift_detected: true` を記録して main thread に報告する（warn: manifest update を要求）。ただし当該 rule-id が「orchestrator-only」と明示されている場合はこの warn を省略してよい。

   ```
   # 検証用簡易判定（概念コード）
   manifest_union = ⋃(role_subset for role in manifest_table)
   superset = required_rules from SKILL.md frontmatter

   if not manifest_union ⊆ superset:
     # manifest が superset 外の未知 rule を参照している
     fail-close: manifest references unknown rule(s): (manifest_union - superset)
   if exists rule in superset such that rule ∉ manifest_union and rule is not explicitly tagged "orchestrator-only":
     # superset に新規追加された rule が manifest に反映されていない可能性
     warn: manifest may be stale, request manifest update
   ```

3. **冪等チェック**: LOOP_STATE の `active_rules:` フィールドを確認する。
   - 既に同 rule-id が記録されている場合 → 再 Read しない（冪等性保証）
   - 未記録の rule-id がある場合 → `.agents/rules/<id>.md` を Read して context に追加する

4. LOOP_STATE の `active_rules:` フィールドに確定した **superset** の rule-id セットを記録する:
   ```yaml
   active_rules: github-ops-workflow, issueops-common-guard, git-policy, file-edit-protocol, skill-sync-policy, issue-uncertainty-policy, skill-rule-boundary, orchestrator-skill-policy, subagent-design-policy
   ```
   マーカー書式: `<active_rules id1, id2, id3>` （カンマ + スペース区切り、rule-id は `[a-z0-9_-]+` 形式）
   検出 regex: `<active_rules ([a-z0-9_-]+(?:, [a-z0-9_-]+)*)>`

5. **SubAgent 委譲時の selective inline 注入（role manifest 適用）**:
   `implementation-worker` / `pr-reviewer` / `adversarial-reviewer` などへ Agent tool で委譲する際、**上記 Role-Rule Manifest テーブルに従って当該 role の subset を抽出し**、`prompt` の冒頭ブロックに inline 展開する:
   ```
   <active_rules github-ops-workflow, issueops-common-guard, git-policy, ...>
   （↑ この SubAgent に注入する subset の rule-id のみを列挙する）

   <rule id="github-ops-workflow">
   （.agents/rules/github-ops-workflow.md の本文）
   </rule>

   <rule id="issueops-common-guard">
   （.agents/rules/issueops-common-guard.md の本文）
   </rule>

   （subset 内の rule 本文のみを同様に展開する。superset の全 rule を展開しない）
   ```
   SubAgent 側からは `.agents/rules/*.md` を自律的に Read しない（既に注入済みのため）。

   **Cache marker 拡張（Alt 4: iteration ごと 1 回 Read）**: 同 iteration 内で複数 SubAgent を委譲する場合、`.agents/rules/<id>.md` の本文 Read を **iteration ごとに 1 回**にまとめる:
   1. iteration 開始時に LOOP_STATE.active_rules（superset）を確定し、対応する rule 本文を 1 度だけ Read して memory にキャッシュする
   2. 各 SubAgent 委譲時は、role manifest から subset を抽出し、キャッシュから該当 rule 本文を取り出して inline 展開する
   3. 同 iteration 内で同一 rule の Read は重複させない（冪等マーカーとは別の運用層の最適化）

6. **`external_research_skip_basis` の inline 注入（F6）**: LOOP_STATE の `external_research_skip_basis` が記録されている場合は、SubAgent prompt に以下も追加する:
   ```
   ## External Research Skip Basis（前 iteration の調査スキップ根拠）
   （LOOP_STATE.external_research_skip_basis の内容）
   ```
   これにより `pr-reviewer` / `adversarial-reviewer` がスキップ判断の妥当性を評価できる。

---

### 事前準備

1. Issue contract を取得する:
   ```bash
   gh issue view <Issue番号> --json title,body,comments
   ```
   - `Outcome` / `Acceptance Criteria` / `Verification Commands` / `Allowed Paths` / `Required Skills` を確認する。

1.25. 関連/類似 Issue を検索し、統合可否を確認する:
   ```bash
   gh issue list --search "<Issueタイトルの主要語句>" --state all --limit 20 --json number,title,state,url,labels
   ```
   - 既存の関連 Issue が実装範囲と重なる場合は、1 PR に収まるかを先に判断する。
   - 統合する場合は、対象 Issue に統合通知コメントを投稿し、本 Issue の Outcome / Acceptance Criteria / Verification Commands に統合後のスコープを反映してから実装に進む。
   - 統合対象が複数ある場合は、重複削減の優先順位と残件を明示し、1 PR で扱う範囲を超えるなら分割して停止する。

   **姉妹 Issue 統合候補の自動検出**: 以下の条件をすべて満たす Issue は「統合候補」として優先的に検討する:
   1. 同一の親 Issue（`Parent Issue:` フィールドまたはラベルで確認）
   2. `change_kind=docs-only`（コード変更なし、ドキュメント追記・修正のみ）
   3. Allowed Paths が対象 Issue と独立しており、ファイル衝突が起きない（independent_file_paths）

   上記3条件を満たす姉妹 Issue が存在する場合、1 PR に統合することでレビューコストと CI 実行回数を削減できる。統合判断手順:
   ```bash
   # 同一親 Issue の OPEN 姉妹 Issue を検索（parent issue 番号で絞り込む）
   gh issue list --search "parent #<親Issue番号>" --state open --limit 20 --json number,title,body,labels
   ```
   - 姉妹 Issue の Allowed Paths と本 Issue の Allowed Paths が完全に独立していることを確認する
   - 独立している場合のみ統合通知コメントを投稿し、1 PR として実装する
   - Allowed Paths に重複がある場合は個別実装を維持する（マージ時の競合リスク回避）

   **`gemini-cli-headless-delegation` を使った類似 scope 探索 prompt 雛形**:

   `gh issue list` の OR query だけでは長い自然語タイトルや多候補の類似 scope を効率的に調査するのが難しい場合がある。`gemini-cli-headless-delegation` に委譲することで判断コストを削減できる。以下の雛形をそのままコピーして使う。

   キーワード抽出ルール（タイトルから 2〜4 個）:
   1. タイトルから名詞・動詞の主要語句（2〜4 個）を抽出する（助詞・接続詞は除く）
   2. 固有名詞・ファイル名・コマンド名はそのまま保持する（例: `gemini-cli-headless-delegation`, `preparation.md`）
   3. 英語キーワードは OR で分割できる
   4. 以下の `gh issue list` OR query で候補を絞り込む:
      ```bash
      gh issue list --search "<keyword1> OR <keyword2> OR <keyword3>" --state all --limit 20 --json number,title,state,url,labels
      ```

   `gemini-cli-headless-delegation` 構造化 request 雛形（`tmp/similar-scope-request.json` に保存して実行）:
   ```json
   {
     "schema": "delegation_request_v1",
     "objective": "下記の候補リストについて、既存 OPEN issues との重複可能性・Allowed Paths 競合・1 PR 統合可否を判定する",
     "tool_profile": "github_research",
     "role": "github_research",
     "instructions": [
       "各候補について gh issue list --search '<keywords>' --state open --limit 20 で関連 OPEN issues を検索する",
       "各候補の想定 Allowed Paths と既存 OPEN issue の Allowed Paths が重複しているかを確認する",
       "重複がある場合は統合推奨 / ない場合は独立実装推奨 / 既に解消済みはスキップと判定する",
       "integration_decision と rationale を YAML 形式で出力する"
     ],
     "context_files": [
       "<REPO_ROOT>/.agents/skills/impl-review-loop/steps/preparation.md"
     ],
     "output_sections": [
       "候補ごとの判定結果（統合推奨 / 独立実装 / スキップ）",
       "existing_issues リスト",
       "integration_decision YAML"
     ],
     "inline_context": "調査候補:\n1. <候補名1>: <概要>\n2. <候補名2>: <概要>\n..."
   }
   ```
   `<REPO_ROOT>` は `$(git rev-parse --show-toplevel)` で取得した絶対パスに置換すること（`gemini-cli-headless-delegation` は isolated temp cwd から実行されるためリポジトリ相対パスは解決されない）。

   実行コマンド:
   ```bash
   uv run python3 .agents/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
     tmp/similar-scope-request.json --output-file tmp/similar-scope-result.json
   ```

   戻り値 YAML schema（`instructions` は 2 件以上必須。`existing_issues` は Allowed Paths 競合がある候補のみ列挙。競合なし候補の省略可）:
   ```yaml
   existing_issues:
     - number: <issue番号>
       title: <issue タイトル>
       state: open | closed
       overlap_with_candidate: <候補名>
       allowed_paths_conflict: true | false
     # Allowed Paths 競合がある候補のみ列挙。競合なし候補は省略可。
   integration_decision:
     candidate_1:
       action: integrate | independent | skip
       reason: <理由>
       target_issue: <統合先 issue 番号 or null>
     # 以降、候補ごとに繰り返す
   rationale: <全体の統合方針サマリー>
   ```

   使用例（PR #2205 cleanup の 4 候補調査）:

   PR #2205 (Issue #2193 / GoogleVisionClient 集約) のマージ後 cleanup フェーズで以下 4 候補を調査した実例。

   request（要点）（`context_files` の `<REPO_ROOT>` は実行前に絶対パスへ置換すること）:
   ```json
   {
     "schema": "delegation_request_v1",
     "objective": "PR #2205 (Issue #2193) cleanup: 実装中に観測した類似 scope の follow-up 候補 4 件について、既存 OPEN issues との重複・1 PR 統合可否を判定する",
     "tool_profile": "github_research",
     "role": "github_research",
     "instructions": [
       "各候補について gh issue list で関連 OPEN issues を検索する",
       "Allowed Paths の重複をチェックし、統合可否を判定する",
       "候補 3（GCV client config_path dead param）は PR #2205 本文・実装を参照し、既に解消済みかを確認する",
       "integration_decision と rationale を YAML 形式で出力する"
     ],
     "context_files": ["<REPO_ROOT>/.agents/skills/impl-review-loop/steps/preparation.md"],
     "output_sections": ["候補ごとの判定", "integration_decision YAML"],
     "inline_context": "調査候補:\n1. impl-review-loop 1.25 prompt 雛形: preparation.md step 1.25 に gemini-cli-headless-delegation 委譲雛形を追加\n2. VC grep false-positive 対策: issue-body-authoring/SKILL.md に grep パターンガイドラインを追記\n3. GCV client 集約余地: GoogleVisionClient の config_path が dead param 状態（PR #2205 で実質解消予定）\n4. live-verify benchmark multi-page 化: live_verify_ocr_density_sweep.py の ground truth multi-page 対応"
   }
   ```

   response（要約）:
   ```yaml
   existing_issues:
     - number: 2131
       title: "improve(issue-body-authoring): スキル divergence 修正 Issue 向け VC grep パターン..."
       state: open
       overlap_with_candidate: "VC grep false-positive 対策"
       allowed_paths_conflict: false
   integration_decision:
     candidate_1:
       action: independent
       reason: "preparation.md への雛形追加は独立スコープ。既存 OPEN issue と Allowed Paths 重複なし。新規 Issue 起票推奨"
       target_issue: null
     candidate_2:
       action: independent
       reason: "issue-body-authoring/SKILL.md への追記。#2131 は related だが Allowed Paths は独立。新規 Issue 起票推奨"
       target_issue: null
     candidate_3:
       action: skip
       reason: "PR #2205 の GoogleVisionClient 集約で config_path dead param は実質解消。追加対応不要"
       target_issue: null
     candidate_4:
       action: independent
       reason: "live-verify benchmark multi-page 化は別 Feature scope。別途起票"
       target_issue: null
   rationale: "4 候補はすべて独立スコープで相互の Allowed Paths 重複なし。候補 3 は PR #2205 で実質完了のためスキップ。残り 3 件はそれぞれ独立した新規 Issue として起票する"
   ```

1.4. canonical PR state を観測する:
   ```bash
   gh pr list --state open --search "\"#<Issue番号>\" in:body" \
     --json number,title,state,url,headRefName,updatedAt
   ```
   - 上記で 0 件の場合は、`headRefName` ベースの secondary lookup を実行する:
     ```bash
     # `head:` qualifier は完全一致のため client-side で prefix match する
     # feat/issue-<N> の完全一致 または feat/issue-<N>- で始まる branch を対象にする
     gh pr list --state open --limit 100 \
       --json number,title,state,url,headRefName,updatedAt \
       | jq --arg n "<Issue番号>" '[.[] | select(.headRefName == "feat/issue-" + $n or (.headRefName | startswith("feat/issue-" + $n + "-")))]'
     ```
   - 両クエリの結果を union して canonical PR 候補とする（`number` で重複排除）。
   - union 後に open PR が 0 件なら `canonical_pr_url: null` とする。
   - 1 件だけなら、その PR を `canonical_pr_url` として LOOP_STATE に記録する。
   - 複数ある場合は、Issue comment / PR comment の `canonical_pr_url` または `Superseded by #<PR番号>` を見て正本を決める。決められない場合は停止する。
   - repair branch を切り直す場合は `repair_context.reason`, `repair_context.previous_pr_url`, `repair_context.mode` をここで確定してから Step 1 へ進む。

1.5. spec-status 観測結果を取得する:
   ```bash
   # 対象 feature 名を issue context / current worktree path / changed paths から解決する
   # そのうえで `.agents/workflows/spec-status.md` を実行し、completed phases と canonical surface を記録する
   ```
   - path 名だけで canonical surface を推測せず、解決できない場合は停止する。
   - spec-status の観測結果と Issue contract の Allowed Paths を突き合わせる。
   - canonical surface と Allowed Paths が一致しない、または scope 判定に必要な feature 名が解決できない場合は、実装に進まず scope delta として扱う。

2. 作業ツリーの clean チェックを実施する:
   ```bash
   git status
   ```
   - **未追跡ファイル（untracked files のみ）がある場合**は以下のいずれかのケースが考えられます:
     - root worktree に checkpoint や中途成果物などの既知 artifact が存在する（cleanup 対象）
     - リポジトリ外のテンポラリファイルが誤作成されている（cleanup 対象）
     **停止条件**: staged / unstaged の変更がある場合は委任を中断し、以下を実施してください:
     1. 警告を出力する（例: `[WARN] 未コミット変更（staged/unstaged）が検出されました。新ブランチに混入する可能性があります。`）
     2. 変更内容を確認し、`git stash`（一時退避）または手動クリーンアップ（`git restore .` / `git reset HEAD <file>` 等）を促す。
     3. 作業ツリーが clean（`nothing to commit, working tree clean`）になってから `implementation-worker` SubAgent への委任を再開する。
     **継続条件**: 未追跡ファイルのみ存在し、staged/unstaged 変更がない場合は委任を継続してください。専用 worktree（Step 3 で新規作成）は main ブランチをベースに独立した作業ツリーを持つため、root worktree の未追跡ファイルは専用 worktree での実装に影響しません（例: root worktree に `checkpoint.json` が残っていても、専用 worktree は clean な main ブランチ上で開始されます）。
   - **補足**: `git checkout -b` で新ブランチを作成しても、unstaged 変更はそのまま新ブランチに持ち越される。このチェックを省略すると、前のブランチの変更が新ブランチの PR に混入するリスクがある（参考: PR #661 / Issue #657 で発生した事例）。
   - `git worktree` を使う場合は main を基点に新 worktree を作成するため、root worktree の未追跡ファイルは新 worktree に混入しない（混入リスクは低い）。ただし staged/unstaged 変更は別途プロセスで管理中のコードであり、新 worktree の base となる main ブランチの整合に影響するため、必ず確認してください。

2.5. worktree 作成前に remote main との整合を確認する（新規作成時のみ）:

   ```bash
   # worktree 作成前に remote main との整合を確認
   git fetch origin main
if ! git merge-base --is-ancestor origin/main main; then
  echo "[WARN] local main が origin/main より古い状態です。git pull origin main を実行してください。"
  exit 1
fi

if ! git rev-parse --is-bare-repository >/dev/null 2>&1; then
  AHEAD_COUNT=$(git rev-list origin/main..main | wc -l)
  if [ "$AHEAD_COUNT" -gt 0 ]; then
    echo "[WARN] local main が origin/main より $AHEAD_COUNT 件先行しています。"
    echo "[WARN] worktree 作成前に先に git push origin main を実行してください。"
    exit 1
  fi
fi
   ```

   - 既存 worktree の**再利用**時はこのチェックを省略してよい（worktree base は既に確定しているため）。
   - **新規作成**時は必ずこのチェックを実施し、`git worktree add ... main` の `main` が remote main と同期していることを確認してから実行すること。
   - `git pull origin main` を実行した場合は、ステップ 2 の clean チェックを再実行すること（pull による差分が発生している可能性があるため）。
   - 参考: PR #797 / Issue #797 において local main が remote より古い状態で worktree を作成した結果、PR ブランチが古い base から分岐し CONFLICTING になった事例。

   **`git worktree add` 直前の base drift 再確認（新規作成時のみ）:**
   `git worktree add ... main` を実行する直前に、再度 merge-base を検証する。fetch から worktree 作成までの間に手動 fetch や長時間の worktree 再利用で base が進んだ場合を検知するため:
   ```bash
   if ! git merge-base --is-ancestor origin/main main; then
     echo "[WARN] git worktree add 直前に base drift を検知しました。git pull origin main を再実行してください。"
     exit 1
   fi
   ```
   このチェックを step 2.5 の整合確認と合わせて 2 回実施することで、fetch 後・worktree 作成前の両タイミングをカバーする。

3. worktree を自動準備または再利用する:

   **branch 命名規則と worktree path の決定:**
   - branch 名: `feat/issue-<N>-<slug>`
   - worktree path: `<repo_root>/wip/worktree-issue-<N>-<slug>`（リポジトリルート内の `wip/` ディレクトリに配置。issue 番号を含めることで異なる Issue 間の path 衝突を防ぐ）
   - slug が空文字の場合（日本語のみのタイトル等）: worktree path は `<repo_root>/wip/worktree-issue-<N>`、branch 名は `feat/issue-<N>` にフォールバックする

   **`<slug>` 生成アルゴリズム:**
   1. Issue title を小文字に変換する
   2. 英数字・ハイフン・スペース以外の文字を削除する
   3. スペースをハイフンに置換する
   4. 連続ハイフンを単一ハイフンに統一する
   5. 先頭・末尾のハイフンを削除する
   6. **50文字を超える場合は単語境界（ハイフン区切り）で切り詰める（ハイフンがない場合は50文字で固定切り詰め）**

   bash ワンライナー:
   ```bash
   SLUG=$(echo "$ISSUE_TITLE" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9 -]/ /g; s/ /-/g; s/--*/-/g; s/^-//; s/-$//')
   # 50文字以上の場合は単語境界（ハイフン区切り）で切り詰める
   # ハイフンが存在しない場合は50文字で固定切り詰め
   if [ ${#SLUG} -gt 50 ]; then
     SLUG=$(echo "$SLUG" | cut -c1-50 | sed 's/-[^-]*$//')
   fi
   # slug が空の場合のフォールバック
   if [ -z "$SLUG" ]; then
     WORKTREE_DIR="worktree-issue-${ISSUE_NUMBER}"
     BRANCH_NAME="feat/issue-${ISSUE_NUMBER}"
   else
     WORKTREE_DIR="worktree-issue-${ISSUE_NUMBER}-${SLUG}"
     BRANCH_NAME="feat/issue-${ISSUE_NUMBER}-${SLUG}"
   fi
   ```

   **既存 worktree の再利用判定（exact path matching）:**
   ```bash
   WORKTREE_ABS=$(realpath "wip/${WORKTREE_DIR}" 2>/dev/null || echo "$(pwd)/wip/${WORKTREE_DIR}")
   git worktree list --porcelain | grep "^worktree " | sed 's/^worktree //' | grep -Fx "$WORKTREE_ABS"
   ```
   - 出力があれば **再利用候補**とし、以下の安全性チェックを実施する。
   - 出力がなければ **新規作成**する:
     ```bash
     git worktree add -b "$BRANCH_NAME" "wip/${WORKTREE_DIR}" main
     ```

   **再利用時の安全性チェック（fail-close）:**
   既存 worktree を再利用する前に、以下の7つのチェックを**必ず**実施する。いずれかが失敗した場合は停止して報告し、quarantine reason または停止理由を残して downstream worker execution に進めない:
   ```bash
   # 1. attached git worktree authenticity チェック: git top-level が worktree 自身であること
   GIT_TOPLEVEL=$(git -C "$WORKTREE_ABS" rev-parse --show-toplevel 2>/dev/null || true)
   if [ "$(realpath "$GIT_TOPLEVEL" 2>/dev/null || echo "$GIT_TOPLEVEL")" != "$WORKTREE_ABS" ]; then
     echo "[FAIL] 再利用候補 path は attached git worktree ではありません: $WORKTREE_ABS"
     echo "[FAIL] git top-level: $GIT_TOPLEVEL"
     exit 1
   fi

   # 2. canonical repo の worktree registry に expected worktree が登録されていること
   CANONICAL_ROOT=$(git rev-parse --show-toplevel)
   if ! git -C "$CANONICAL_ROOT" worktree list --porcelain | awk '/^worktree / {print substr($0,10)}' | while read -r wt; do realpath "$wt"; done | grep -Fx "$WORKTREE_ABS" >/dev/null; then
     echo "[FAIL] 再利用候補 path が canonical repo の worktree registry に存在しません: $WORKTREE_ABS"
     echo "[FAIL] canonical_repo_root: $CANONICAL_ROOT"
     exit 1
   fi

   # 3. git-common-dir チェック: canonical repo と同じ git common dir を共有していること
   CURRENT_COMMON=$(git -C "$WORKTREE_ABS" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
   CANONICAL_COMMON=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
   if [ "$CURRENT_COMMON" != "$CANONICAL_COMMON" ]; then
     echo "[FAIL] 再利用候補 path の git-common-dir が canonical repo と一致しません: $WORKTREE_ABS"
     echo "[FAIL] current_git_common_dir: $CURRENT_COMMON"
     echo "[FAIL] canonical_git_common_dir: $CANONICAL_COMMON"
     exit 1
   fi

   # 4. branch identity チェック: expected branch と一致すること
   CURRENT_BRANCH=$(git -C "$WORKTREE_ABS" branch --show-current 2>/dev/null || true)
   if [ "$CURRENT_BRANCH" != "$BRANCH_NAME" ]; then
     echo "[FAIL] 再利用候補の branch が expected branch と一致しません: $WORKTREE_ABS"
     echo "[FAIL] current_branch: $CURRENT_BRANCH"
     echo "[FAIL] expected_branch: $BRANCH_NAME"
     exit 1
   fi

   # 5. dirty 状態チェック: 未コミット変更がないこと
   DIRTY=$(git -C "$WORKTREE_ABS" status --short)
   if [ -n "$DIRTY" ]; then
     echo "[FAIL] 再利用候補の worktree に未コミット変更があります: $WORKTREE_ABS"
     echo "$DIRTY"
     exit 1
   fi

   # 6. origin/main ancestry チェック: origin/main（なければ main）と HEAD が共通祖先を持つこと
   BASE_REF=main
   git -C "$WORKTREE_ABS" rev-parse --verify origin/main >/dev/null 2>&1 && BASE_REF=origin/main
   if ! git -C "$WORKTREE_ABS" merge-base "$BASE_REF" HEAD >/dev/null 2>&1; then
     echo "[FAIL] 再利用候補の worktree が $BASE_REF と共通 merge-base を持ちません: $WORKTREE_ABS"
     echo "[FAIL] unexpected init commit ancestry または別履歴 repo の可能性があります。"
     exit 1
   fi

   # 7. exact bad head チェック: known bad head そのものに載っていないこと
   CURRENT_HEAD=$(git -C "$WORKTREE_ABS" rev-parse HEAD 2>/dev/null || true)
   if [ "$CURRENT_HEAD" = "cdd529c363170e6f2202095cc01d490d43eee0b2" ]; then
     echo "[FAIL] 再利用候補の worktree が known bad head に一致しました: $WORKTREE_ABS"
     echo "[FAIL] current_head: $CURRENT_HEAD"
     echo "[FAIL] quarantine reason: stale bad head retained from poisoned worktree reuse"
     exit 1
   fi
   ```

   **同一 Issue の worktree 判定基準:**
   - worktree path に `worktree-issue-<N>` が含まれている（issue 番号で一意に特定）場合に同一 Issue の worktree とみなす。

   **注意事項:**
   - worktree 作成後、`expected_branch` と `expected_worktree_path` を確定させてから次ステップへ進む。
   - 新規作成時も、Step 1 に進む前に `git -C "$WORKTREE_ABS" branch --show-current` が `"$BRANCH_NAME"` と一致すること、`git -C "$WORKTREE_ABS" rev-parse --show-toplevel` が `"$WORKTREE_ABS"` と一致すること、`git -C "$(git rev-parse --show-toplevel)" worktree list --porcelain` に `"$WORKTREE_ABS"` が登録されていること、`git -C "$WORKTREE_ABS" rev-parse --path-format=absolute --git-common-dir` が canonical repo と一致すること、`origin/main`（なければ `main`）と共通 merge-base を持つことを確認する。`wip/worktree-*` 風のディレクトリでも attached git worktree でなければ fail-close とする。
   - clean な stale bad head は dirty 状態チェックや ancestry チェックをすり抜けることがあるため、`git rev-parse HEAD` による exact head 照合を省略しない。`outside.txt` / `wip/demo/module.py` のような dirty signature が見えない run でも、known bad head そのものなら quarantine reason または停止理由を残して worker execution に進めない。
   - `#2003` で観測された retained poisoned residue `feat/issue-1979-low-output-threshold-rerun-suite` / `/home/squne/projects/KindleAudiobookMakeSystem/wip/worktree-issue-1979-low-output-threshold-rerun-suite` / `cdd529c363170e6f2202095cc01d490d43eee0b2` / `outside.txt` / `wip/demo/module.py` は bootstrap/reuse fail-close の regression target として扱い、同じ signature を見つけた run は quarantine reason または停止理由を残して worker execution に進めない。

4. LOOP_STATE を初期化して Issue にコメントする:
   ```bash
   gh issue comment <Issue番号> --body "$(cat <<'EOF'
   ## LOOP_STATE
   \`\`\`yaml
   iteration: 0
   phase: init
   status: running
   pr_url: null
   last_verdict: null
   active_rules: []
   canonical_pr_url: null
   canonical_pr_source: null
   superseded_prs: []
   \`\`\`
   EOF
   )"
   ```

4.5. drift signature と canonical evidence bundle の capture 契約を確定する:
   - attached worktree / branch が期待 head から外れた場合は、root cause 推測より先に以下を回収する:
     ```bash
     git reflog --date=iso --all
     git worktree list
     git status --short --branch
     git rev-parse HEAD
     git branch --show-current
     ```
   - issue / worktree の state と一緒に、`expected_head`, `expected_branch`, `current_head`, `current_branch` を comment か handoff artifact に残す。
   - drift signature は `unexpected init commit ancestry`, `outside.txt`, `wip/demo/module.py` のような poisoned tree 症状を最低 1 つ以上書く。
   - verify 用 detached head に切り替える場合は、その decision と理由を `detached verify head switch decision` として残す。
   - ここで残すのは workflow evidence capture であり、profile routing の実装修正は `#1948 / PR #1977`、`pre-push` helper cancel/ignore は `#1978` の境界を維持する。

5. `.agents/skills/` 配下のファイルを変更する場合は、まず `.agents/skills/.sync-exclude` を確認し、対象スキルが同期除外かどうかを判定する:

   > `impl-review-loop` は `.agents/skills/.sync-exclude` に含まれるため、`.claude/skills/impl-review-loop/**` へ同期しない。

   ```bash
   # sync 除外対象の確認
   grep -n "^impl-review-loop$" .agents/skills/.sync-exclude
   # impl-review-loop 編集時は .agents と手動 mirror の .claude を両方 add する
   git add .agents/skills/impl-review-loop/SKILL.md .agents/skills/impl-review-loop/steps/*.md
   git add .claude/skills/impl-review-loop/SKILL.md .claude/skills/impl-review-loop/steps/*.md
   ```

   - `impl-review-loop` 以外のスキルでは従来どおり `bash scripts/sync-agent-skills.sh` / `--check` を実施する。
   - `impl-review-loop` 編集時は `.claude/skills/impl-review-loop/**` を手動 mirror で揃え、`bash scripts/sync-agent-skills.sh --check` は sync 対象外であることを崩さずに他 skill の drift がないことだけを確認する。
   - PR 本文には `impl-review-loop` が sync-exclude であり、mirror は手動更新したことを evidence として残す。

