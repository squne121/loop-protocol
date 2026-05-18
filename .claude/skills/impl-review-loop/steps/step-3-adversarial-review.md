### Step 3: 敵対的レビュー（`adversarial-reviewer` SubAgent）

**実行条件: TEST_VERDICT が PASS または PARTIAL の場合のみ実行する。FAIL の場合はスキップして Step 5 へ進む。**

#### Step 3 の責務境界（相互参照禁止）
adversarial-reviewer の責務: 信頼性・セキュリティリスクのレビュー（race condition / rollback / auth / observability 等）+ project convention 適合観点（architecture-fit）
**pr-reviewer（Step 4）の結果を参照しないこと（相互参照禁止）。**

pr-reviewer と adversarial-reviewer は依存なしで並列起動可。

**architecture-fit 必須確認項目（委任プロンプトに明記すること）:**
- `.agents/`, `.claude/`, `.codex/`, `justfile`, `pyproject.toml` 等の正規 tooling が使われているか
- ad-hoc directory / file（例: `.agents/runtime/`, `.agents/cache/` 等）が無断追加されていないか
- AI 製品仕様外の `.yaml` / `.json` registry を直接 parse する構造になっていないか
- 既存 project tooling（`just` / `uv` 等）で代替できる実装が ad-hoc に書かれていないか
- 指摘は `[ARCH-FIT]` プレフィックスを付与し、severity 基準は `adversarial-review` SKILL.md の「architecture-fit」セクションに従うこと

#### Step 3 と Step 4 の並列実行

Step 2（test-runner）完了後、TEST_VERDICT が PASS または PARTIAL の場合、
Step 3（adversarial-reviewer）と Step 4（pr-reviewer）を**並列独立実行**する。
両 SubAgent は互いのレビュー結果を参照しない。

`adversarial-review` スキルに従って PR diff をレビューし、結果を PR にコメントする。

**SubAgent への必須渡し情報（事前取得してプロンプトに展開）:**
- `model` / `model_reasoning_effort`: `model_overrides["adversarial-reviewer"]` に指定がある場合は、その値を CodexCLI SubAgent 委譲プロンプトへ明示して渡す

> **Issue contract の In Scope / Out of Scope をインライン展開して渡すこと（必須）:**
> オーケストラレータは委譲前に Issue contract から In Scope / Out of Scope リストを取得し、adversarial-reviewer への委任プロンプトにインライン展開して渡すこと。
> これにより adversarial-reviewer が「Issue contract が要求している変更」と「実装上の問題」を区別できる。
>
> **Issue contract AC 明示要求の扱いガイダンス（必ず委任プロンプトに含めること）:**
> Issue contract の Acceptance Criteria に明示されているフィールド値・変更（例: `implementation_status: "complete"` の設定、特定の文字列の追加・削除など）については、「契約設計の懸念」として分類し（MEDIUM/LOW 以下）、実装品質の問題として CRITICAL/HIGH に分類しないこと。
> AC 明示要求の対応は実装者の判断ではなく、Issue contract 策定時にレビュー済みの合意事項である。

> **委任プロンプトに必ず明示する作業ツリー条件:**
> - `expected_worktree_path: <absolute path>` を必ず渡し、その worktree 内のファイルのみ確認すること。
> - root worktree は PR 適用前の状態なので参照禁止とする。
> - 変更ファイルの全文は `tmp/pr${PR_NUMBER}_skill_v${ITERATION}.md` からインライン展開して渡し、要約版や抜粋版だけで委任しないこと。

> **ADV_VERDICT コメント投稿は必須。`gh pr comment` の実行を省略しないこと:**
> adversarial-reviewer SubAgent は、レビュー結果を必ず `gh pr comment` で PR にコメントする必須責務を持つ。コメント投稿を省略すると、オーケストラレータの Step 3.5 正規化および Step 5 判定が機能しないため、絶対に省略してはならない。

<!-- Step 3 委譲前にオーケストラレータが実行する pre-fetch -->
オーケストラレータが以下のコマンドを実行し、結果をすべてプロンプトにインライン展開して渡すこと:
```bash
# レビュー対象 PR head SHA を固定記録する（Step 5 終了判定時のガード用）。
# この値はループ全体で保持し、pr-reviewer には LOOP_VERDICT YAML に
# reviewed_head_sha: <SHA> として記載させること（詳細は Step 4 参照）。
REVIEWED_HEAD_SHA=$(gh pr view <PR番号> --json headRefOid -q .headRefOid)
echo "[impl-review-loop] REVIEWED_HEAD_SHA=$REVIEWED_HEAD_SHA"

gh pr diff <PR番号>
gh pr diff <PR番号> --name-only
gh pr view <PR番号> --json body -q .body
# 変更ファイルごとに全内容を取得（"whack-a-mole" パターンを防ぐため、diff のみでなくファイル全体を読ませること）
# PR head ブランチ名を取得して revision を固定する（ローカル HEAD との不整合を防ぐ）
PR_BRANCH=$(gh pr view <PR番号> --json headRefName -q .headRefName)
while IFS= read -r f; do
  echo "=== $f ==="
  git show "origin/$PR_BRANCH:$f" 2>/dev/null || echo "[ファイルが削除またはバイナリのためスキップ]"
done < <(gh pr diff <PR番号> --name-only)
```
- `gh pr diff <PR番号>` の結果は `tmp/pr<PR番号>_v<iteration>.diff` から、変更ファイル全文は `tmp/pr<PR番号>_skill_v<iteration>.md` から inline 展開して渡すこと。要約版や抜粋版は使わないこと。
- 「PR ブランチ上の実装が正しければ、main との差分は HIGH として扱わないこと（PR 未マージ状態は正常）」
- 変更ファイルが `wip/` 配下のみで、Issue contract が multi-thread / cross-process coordination / retry-safe mutation を要求していない場合は、multi-thread safety / race condition / TOCTOU 系の指摘を **自動ループ継続条件の対象にしない**。重大なデータ破損・不可逆変更の実証がない限り、severity は **MEDIUM 以下**として報告させること。
- **REVIEWED_HEAD_SHA の永続化と iteration 更新**: `REVIEWED_HEAD_SHA` はレビューと終了判定で同一 revision を参照するための正本。**iteration を跨ぐ際は必ず新 pre-fetch で再取得して上書きすること**。古い SHA を再利用すると Step 5「未レビュー commit ガード」が `reviewed_head_sha == 現在の PR head SHA` を誤って成立させ、本ガードを bypass して PR #744 / Issue #707 と同じ「未レビュー commit が main にマージされる」事故が再発する。Step 2+3 への委譲前に必ず最新の head SHA を再取得すること。

**キャッシュ命名規則と iteration 間再利用ルール:**

オーケストラレータが上記 pre-fetch コマンドを複数 iteration で繰り返し実行する場合は、取得結果を以下の命名規則でリポジトリ配下の `tmp/` にキャッシュして再利用できます:

- `tmp/pr<PR番号>_v<iteration>.diff` — PR diff 本体（`gh pr diff <PR番号>` の出力）
- `tmp/pr<PR番号>_v<iteration>.files` — 変更ファイル名リスト（`gh pr diff <PR番号> --name-only` の出力）
- `tmp/pr<PR番号>_body_v<iteration>.md` — PR 本文（`gh pr view <PR番号> --json body -q .body` の出力）
- `tmp/pr<PR番号>_skill_v<iteration>.md` — PR 変更ファイル全体の連結結果（複数ファイルを `=== <path> ===` ヘッダー付きで 1 つの markdown に集約したもの）

**iteration 間キャッシュ再利用ルール:**

- **同 iteration 内の再利用**: 同一 iteration 内であれば test-runner / adversarial-reviewer 両 SubAgent が上記キャッシュを参照してよい（API 負荷軽減、consistency 確保）。
- **iteration 切替時の更新手順**: iteration を切り替える際は以下の手順でキャッシュを更新してから委譲すること:
  ```bash
  # 最新の PR head ブランチを取得して fetch
  PR_BRANCH=$(gh pr view <PR番号> --json headRefName -q .headRefName)
  git fetch origin "$PR_BRANCH"
  # 次の iteration 用に新しいバージョンのキャッシュを取得
  NEXT_ITERATION=$((CURRENT_ITERATION + 1))
  mkdir -p tmp
  gh pr diff <PR番号> > tmp/pr<PR番号>_v${NEXT_ITERATION}.diff
  gh pr diff <PR番号> --name-only > tmp/pr<PR番号>_v${NEXT_ITERATION}.files
  gh pr view <PR番号> --json body -q .body > tmp/pr<PR番号>_body_v${NEXT_ITERATION}.md
  # ファイル全体を更新取得
  while IFS= read -r f; do
    echo "=== $f ===" >> tmp/pr<PR番号>_skill_v${NEXT_ITERATION}.md
    git show "origin/$PR_BRANCH:$f" 2>/dev/null >> tmp/pr<PR番号>_skill_v${NEXT_ITERATION}.md || \
      echo "[ファイルが削除またはバイナリのためスキップ]" >> tmp/pr<PR番号>_skill_v${NEXT_ITERATION}.md
  done < tmp/pr<PR番号>_v${NEXT_ITERATION}.files
  ```
- **旧キャッシュの扱い**: 前 iteration のキャッシュ（例: `_v1.diff`）は残しておくが、次 iteration（例: `_v2` 以降）では参照しない（stale 判定）。iteration がロールバックまたは再試行された場合は、該当 version のキャッシュを削除してから再取得すること（同一 version の stale 参照を防ぐ）。

**実装例（bash スニペット）:**

```bash
# キャッシュファイルパスの定義
ITERATION=1
PR_NUMBER=123
CACHE_DIFF="tmp/pr${PR_NUMBER}_v${ITERATION}.diff"
CACHE_FILES="tmp/pr${PR_NUMBER}_v${ITERATION}.files"
CACHE_BODY="tmp/pr${PR_NUMBER}_body_v${ITERATION}.md"
CACHE_SKILL="tmp/pr${PR_NUMBER}_skill_v${ITERATION}.md"

# キャッシュが存在しない場合は新規取得
if [ ! -f "$CACHE_DIFF" ]; then
  mkdir -p tmp
  gh pr diff $PR_NUMBER > "$CACHE_DIFF"
  gh pr diff $PR_NUMBER --name-only > "$CACHE_FILES"
  gh pr view $PR_NUMBER --json body -q .body > "$CACHE_BODY"
  
  # ファイル全体を取得
  PR_BRANCH=$(gh pr view $PR_NUMBER --json headRefName -q .headRefName)
  > "$CACHE_SKILL"  # 初期化
  while IFS= read -r f; do
    echo "=== $f ===" >> "$CACHE_SKILL"
    git show "origin/$PR_BRANCH:$f" 2>/dev/null >> "$CACHE_SKILL" || \
      echo "[ファイルが削除またはバイナリのためスキップ]" >> "$CACHE_SKILL"
  done < "$CACHE_FILES"
fi

# キャッシュを SubAgent に渡す際はファイル内容をプロンプトにインライン展開
DIFF_CONTENT=$(cat "$CACHE_DIFF")
# ... SubAgent プロンプトに DIFF_CONTENT を展開して渡す
```

**キャッシュ活用時の注意:**
- Step 3 委譲時に上記キャッシュを使用する場合は、必ず同一 iteration のキャッシュを使い分けること（例: iteration 1 では `_v1.diff` のみ）。
- iteration 切替時には新規 version で再取得し、旧キャッシュは参照しない。
- キャッシュファイルがない場合は上記 pre-fetch bash block をそのまま実行して新規取得してよい。


**adversarial-reviewer の手順:**
1. PR diff・変更ファイル・PR 本文を分析する。
2. CRITICAL / HIGH / MEDIUM / LOW 件数を分類する。
3. **修正提案を行う際は、提案した修正が新たな問題を引き起こさないかクロスチェックすること**（例: 変数スコープの変更、例外パスの追加など、修正後の状態でも整合性が保たれるか確認する）。
4. 変更パスが `wip/` のみで、かつ Issue contract に concurrency 保証が含まれない場合は、multi-thread safety / race condition / TOCTOU 指摘を CRITICAL/HIGH にしない。報告する場合は「WIP 単一スレッド前提では自動ループ継続対象外」と明記する。
5. 結果を PR にコメントする:
   ```bash
   gh pr comment <PR番号> --body "## Adversarial Review Report
   ### Findings Summary
   - CRITICAL: <N>件
   - HIGH: <N>件
   - MEDIUM: <N>件
   - LOW: <N>件
   ### Baseline Failure（既知の既存問題）
   <!-- baseline failure: main ブランチ上に既存する問題・技術的負債・既知の不具合で、今回の差分とは無関係のもの -->
   <!-- 実装者は今回の PR では修正不要。必要なら別 Issue として追跡する -->
   - <既知問題の一覧、または「なし」>
   ### 今回差分 Blocker（今回の変更に起因する問題）
   <!-- diff blocker: 今回の PR diff が直接引き起こしている CRITICAL/HIGH 問題 -->
   <!-- 実装者がこの PR で修正すべき対象。ループ継続の判断に使う -->
   - <CRITICAL/HIGH 件数と内容、または「なし」>
   ### Details
   <詳細（全 finding の内訳）>
   ### Verdict
   ADV_VERDICT: APPROVED / NEEDS_FIX"
   ```

Step 3 完了後（または TEST_VERDICT: FAIL により Step 3 をスキップした場合）、オーケストラレータが LOOP_STATE を更新する（TEST_VERDICT / ADV_VERDICT の合否に関わらず `phase: tested` へ遷移）:
```bash
gh issue comment <Issue番号> --body "$(cat <<'EOF'
## LOOP_STATE
\`\`\`yaml
iteration: <N>
phase: tested
status: running
pr_url: <PR_URL>
last_verdict: null
\`\`\`
EOF
)"
```

---


**SubAgent レート制限フォールバック:**

`adversarial-reviewer` SubAgent がレート制限（エラーまたは空レスポンス）に達した場合、オーケストラレータは以下を実施する:

1. レート制限を検出し、`adversarial-reviewer` SubAgent を直接呼び出す。
2. 同じ pre-fetch データをインライン展開して渡し、PR diff をレビュー・分類する。
3. 結果を PR にコメントする（`ADV_VERDICT` 形式）。
4. LOOP_STATE に記録する。

### Step 3.5: 敵対的レビュー所見の正規化（矛盾 / WIP スコープ / 既知 Out of Scope）

Step 3.5 は Step 3（adversarial-reviewer）完了後に即実行し、Step 4 と**並行して実行される**正規化ステップ。
Step 3.5 は Step 4 の完了を待たない。

Step 3 完了後、オーケストラレータは Step 4 実行判定に入る前に adversarial-reviewer の所見を**そのまま件数評価しない**。以下の順で正規化する。

> **Baseline vs 今回差分の分離について**: Step 3 の adversarial-reviewer が出力した「Baseline Failure（既知の既存問題）」セクションに分類された所見は、自動的に `out_of_scope_findings` 候補として扱う。ただし「既存問題」かどうかの最終判定はオーケストラレータが行う（adversarial-reviewer が誤分類する可能性があるため）。正規化後の `normalized_critical_count` / `normalized_high_count` には「今回差分 Blocker」セクションの件数のみを反映させること。

1. **前回 feedback と最新 ADV コメントを照合する**:
   - 最新の `## Feedback` コメントと、今回の `Adversarial Review Report` を並べて確認する。
   - 判定対象は「前回の修正指示に対して今回が逆方向の修正を要求しているか」「前回 Out of Scope と記録済みの指摘をそのまま再掲しているか」の 2 点に限定する。

2. **矛盾指摘を検出する**:
   - 同一のファイル / 近接行 / 同一論点に対し、前 iteration で「A に変更せよ」と指摘し、その対応後に今回「A から元に戻せ」と指摘した場合は `contradiction_findings` に追加する。
   - 同一 reviewer が `logger.info -> warning` のような変更を要求し、次 iteration で同一箇所に `warning は不適切なので info/debug に戻せ` と要求するケースを典型例とする。
   - `contradiction_findings` に入れた所見は **CRITICAL/HIGH 集計から除外**し、PR コメント**と** LOOP_STATE の両方に「矛盾指摘として自動除外した」ことを残す。

3. **WIP スコープ外の重大指摘を減衰する**:
   - `Changed Paths` が `wip/` 配下のみ、かつ linked issue の `Outcome` / `AC` / `Verification Commands` に concurrency, locking, multi-process safety, retry-safe write が含まれない場合、multi-thread safety / race condition / TOCTOU 指摘は `wip_scope_downgraded_findings` に追加する。
   - このリストに入れた所見は **Step 4 の進行判定では MEDIUM 扱い**とし、CRITICAL/HIGH 集計から除外する。
   - ただし「現在の単一スレッド CLI 実装でもデータ破損や不可逆変更が即時発生する」ことが diff と contract から具体的に示せる場合は除外しない。

4. **前回 Out of Scope 判定済み所見を再掲除外する**:
   - オーケストラレータが過去の LOOP_STATE / Feedback で Out of Scope と明示した所見は `out_of_scope_findings` に保持する。
   - 同一または実質同一の所見が再掲された場合は `repeated_out_of_scope_findings` に追加し、CRITICAL/HIGH 集計から除外する。
   - 一致判定は「同じ論点」「同じ対象パス」「同じ修正要求の方向」を満たすかで行い、文言の細かな揺れだけでは別件扱いにしない。

5. **正規化後件数で Step 4 実行可否を決める**:
   - Step 4 の `CRITICAL/HIGH 件数が 0 件` 判定は、生の adversarial-reviewer 件数ではなく `normalized_critical_count` / `normalized_high_count` を使う。
   - 以下を LOOP_STATE に残してから Step 4 へ進む:
     ```yaml
     contradiction_findings:
       - <summary>
     # 各所見には finding_id（stable ID）と scope_classification が付与される（Issue #794）
     out_of_scope_findings:
       - <summary>
     # 各所見には finding_id（stable ID）と scope_classification が付与される（Issue #794）
     repeated_out_of_scope_findings:
       - <summary>
     # 各所見には finding_id（stable ID）と scope_classification が付与される（Issue #794）
     wip_scope_downgraded_findings:
       - <summary>
     # 各所見には finding_id（stable ID）と scope_classification が付与される（Issue #794）
     normalized_critical_count: <N>
     normalized_high_count: <N>
     ```
   - PR コメントにも「raw 件数」と「normalized 件数」の両方を残し、特に `contradiction_findings` は要約を**必ず列挙**して何を自動除外したかを追跡可能にする。

6. **Normalized findings の structured comment 記録は Step 4（pr-review-judge）へ委譲（Issue #795）**:
   - Step 3.5 では adversarial-reviewer の所見を正規化し、findings を分類する。
   - **structured comment 記録（FINDING_REF タグの埋め込み）は Step 4 で実行される**。詳細は `.agents/skills/pr-review-judge/SKILL.md` セクション 5.5 を参照。
   - フォーマット: `<!-- FINDING_REF finding_id=<ID> scope=<SCOPE> -->`
   - 埋め込み位置: pr-review-judge の verdict コメント内の「judgment blocker リスト」直後に配置する（1行 = 1 finding）。
   - pr-review-judge が以下を実行：
     1. adversarial-reviewer の `Adversarial Review Report` コメントから findings を抽出。
     2. 各 finding に対し、review-output-schema で採番された finding_id と scope_classification を紐付け。
     3. verdict コメント内の blocker リスト直後に `<!-- FINDING_REF finding_id=XXX scope=YYY -->` タグを配置。
     4. `gh pr review --comment` で verdict コメント投稿（self-authored の場合）。
   - **注意**: Step 3 時点では finding_id / scope_classification は不確定（null）の可能性があるため、Step 3.5 正規化で仮採番し、Step 4 で最終確定する（段階的 ID 確定パターン）。
