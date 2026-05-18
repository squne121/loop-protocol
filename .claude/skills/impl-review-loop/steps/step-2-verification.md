### Step 2: 検証（`test-runner` SubAgent）

Step 1 の PR URL が確定したら、まず Step 2（test-runner）を実行する。TEST_VERDICT 確認後、PASS または PARTIAL の場合のみ Step 3（adversarial-reviewer）を実行する。TEST_VERDICT: FAIL の場合は Step 3 をスキップして Step 5（判定）へ進む。

`implement-issue` の verify 手順（Verification Commands / `just check`）を実行し、結果を PR にコメントする。

### Step 2 補助契約（LOCAL_CI_RESULT）

- `scripts/local-ci/dispatch.py` が出力した `LOCAL_CI_RESULT` は `head_sha`, `context`, `command`, `exit_code`, `status_state`, `artifact` を必須要件として扱う（`context` は `local-ci/just-check`）。
- Step 2 実行結果の PR 反映時には `pr-review-judge` が参照できる形で `LOCAL_CI_RESULT` を `local-ci/just-check` の `just check` 結果としてコメント evidence に残す。
- `head_sha` は PR head SHA と照合し、**不一致時は差分レビューを再実行**する（`AC4`/`AC5` の準拠）。

**SubAgent への必須渡し情報:**
- PR URL / PR番号
- Issue番号
- Verification Commands（Issue contract から必ずインライン展開して渡すこと）
- 「Windows ネイティブ検証など、ユーザー環境へ影響がある操作を実行する前に同意を取得すること」
- **「TEST_VERDICT コメントの投稿は必須。`gh pr comment` の実行を省略しないこと」**
- `model` / `model_reasoning_effort`: `model_overrides["test-runner"]` に指定がある場合は、その値を CodexCLI SubAgent 委譲プロンプトへ明示して渡す

**test-runner の役割境界（厳守）:**
- test-runner SubAgent の role は「検証コマンドを実行してレポートする」のみである。
- **コード変更・コミット・push は一切行わないこと**（`.noqa` コメント追加・lint 自動修正・テスト修正を含む）。
- lint エラー・テスト失敗等が検出された場合は修正せず、「今回差分 Blocker」として `TEST_VERDICT: FAIL` で報告すること。
- 上記 role 境界は絶対であり、いかなる理由（CI を通すため、軽微な修正だから等）も例外にならない。

**test-runner の手順:**
1. Verification Commands を実行する。
   - **引数必須 recipe の扱い**: `just live-verify <script>` のように引数（スクリプトパス等）が必須の recipe は、引数なしでは実行不可である。このような recipe は実行を試みず、`SKIP: 引数必須 (例: just live-verify <script>)` として TEST_VERDICT コメントに明示する。
   - **代替 Evidence の参照**: 引数必須 recipe の検証が SKIP になった場合、`wip/` 内の既存レポートファイル（例: `wip/reports/` や `wip/*.json`）が存在する場合はそれを代替 Evidence として参照し、コメントに該当パスと内容の要約を記載する。
   - **Issue 策定ガイドライン**: 引数付き実行が必要な Verification Commands を含む Issue は、策定時に引数の具体例（例: `just live-verify tools/live_verify_sample.py`）を明記することで test-runner が実行可能な形式にすること。
   - **Memory Safety（Issue #1127 iter8）**: `just check` 全体が memory-safe（ulimit -v 4194304 + timeout 1200s + pytest --timeout=60）に実行されるため、hazard 専用 recipe は不要。すべてのテストが安全な resource limit 下で実行される。
2. `just check` を実行する（`just check` 対象外の場合は理由を記録）。
3. 対象コードに `uiautomation` / `pywinauto` 等の Windows GUI 操作が含まれる場合、または Issue contract の Verification Commands に `# live-verify: required` マーカーが含まれる場合は、**下記「Windows GUI 操作を含む Issue の live 検証」手順**を実行する。
4. 結果を PR にコメントする:
   ```bash
   gh pr comment <PR番号> --body "## Test Runner Report
   ### Verification Commands
   \`\`\`
   <実行結果>
   \`\`\`
   ### Mergeability Check
   - mergeable: <MERGEABLE / CONFLICTING / UNKNOWN>
   - mergeStateStatus: <CLEAN / UNSTABLE / BLOCKED / DIRTY など>
   <!-- CONFLICTING / DIRTY / BLOCKED の場合は明示して TEST_VERDICT: FAIL で報告 -->
   ### just check
   exit code: <0 or 非0>
   ### Live Verification
   <live 検証結果、または「対象外」「保留」の理由>
   ### Baseline Failure（既知の既存失敗）
   <!-- baseline failure: main ブランチ上で既に失敗していた既知テスト、環境依存の既存エラー等 -->
   <!-- 今回の差分とは無関係の既存問題。実装者は修正不要。次アクション: 別 Issue で追跡 -->
   - <既存失敗の一覧、または「なし」>
   ### 今回差分 Blocker（今回の変更に起因する失敗）
   <!-- diff blocker: 今回の PR で導入・破壊された失敗。実装者が今すぐ直すべき対象 -->
   - <今回差分による blocker の一覧、または「なし」>
   ### TEST_VERDICT
   \`\`\`yaml
   verdict: PASS / PARTIAL / FAIL
   mergeable: MERGEABLE / CONFLICTING / UNKNOWN  # PR の merge 可能性
   baseline_only: true / false  # true の場合、失敗は baseline failure のみで今回差分 blocker なし
   \`\`\`
   <!-- PARTIAL: 通常テスト PASS だが live 検証未実施（live 検証保留） -->
   <!-- FAIL: CONFLICTING / mergeable 問題 / baseline failure / 今回差分 blocker のいずれかを含む -->
   <!-- baseline_only: true の場合、PR 外の既存問題のため、実装者は修正不要だが人間判断の halt が必要 -->

   ### Normalized Findings Reference
   
   FINDING_REF 埋め込みの仕様（フォーマット・配置位置・生成条件等）は、**正本として `pr-review-judge/SKILL.md` の Step 5.5「normalized findings の structured comment 記録」を参照すること**。
   
   本セクションは仕様の正本ではなく、参照先は以下の通り:
   - **フォーマット仕様**: `pr-review-judge/SKILL.md` Step 5.5「normalized findings の structured comment 記録」（anchor: `#normalized-findings-structured-comment`）の「Structured Comment フォーマット仕様（Section 5.5 補足）」
   - **配置位置・生成条件**: `pr-review-judge/SKILL.md` Step 5.5「normalized findings の structured comment 記録」（anchor: `#normalized-findings-structured-comment`）の「配置位置」「生成条件」「適用タイミング」セクション
   
   **実装上の注意**: test-runner は TEST_VERDICT YAML のみを機械的に生成するため、structured comment の埋め込みは **オーケストラレータ（impl-review-loop）** の責務である。Step 3（adversarial-review 実行）後に、オーケストラレータが FINDING_REF タグを追記する（コメント編集で実装）。
   ```

#### Windows GUI 操作を含む Issue の live 検証

対象コードに `uiautomation` / `pywinauto` 等の Windows GUI 操作が含まれる場合、または Required Skills に `windows-gui-dev` が含まれる場合、または **Issue contract の Verification Commands に `# live-verify: required` マーカーが含まれる場合**:

> **重要**: `windows-gui-dev` を Required Skills に含む Issue の GUI 操作は、**オーケストラレータが直接実行する**。test-runner SubAgent への GUI 操作委任は禁止。詳細は Guardrails セクションを参照。

**オーケストラレータが直接実行する手順:**

1. `just live-verify-preflight` で WSL2 interop 状態を確認する（オーケストラレータが Bash ツールで実行）。
2. 既存の live 検証スクリプト（`tools/live_verify_*.py`）を確認する。
3. GUI 操作の実行前に、**AskUserQuestion（または本文中質問）でユーザー確認を取る**:
   - 確認事項の例: 「Kindle アプリが起動・書籍オープン状態か」「GUI 操作を今すぐ実行してよいか」
   - ユーザーが「Yes」「OK」等を返した後にのみ次のステップへ進む。
4. ユーザー承認後、オーケストラレータが Bash ツールで `powershell.exe -Command` を直接実行して GUI 操作を行う:
   ```bash
   powershell.exe -Command "<Windows GUI 操作スクリプト>"
   ```
5. 証拠 JSON を `reports/live-verification/` に保存して PR にコメントする。

**背景**: SubAgent の継続不可問題（Claude Code メイン会話から SendMessage が利用できないため SubAgent を継続できない）および確認タイミングのずれ（GUI 実行直前ではなく早い段階で確認が入り、ユーザーの Kindle 状態が変わるリスク）を解消するため、GUI 操作はオーケストラレータが直接担当する（Issue #912）。

**TEST_VERDICT の決定ルール:**

| 条件 | TEST_VERDICT |
|---|---|
| 通常テスト PASS かつ live 検証 PASS | `PASS` |
| 通常テスト PASS かつ live 検証未実施（Windows GUI 対象外、または環境要因で実施不可） | `PARTIAL`（live 検証保留を明示） |
| いずれかのテスト FAIL | `FAIL` |

> **注意**: `TEST_VERDICT: PARTIAL` の場合は PR コメントに「live 検証保留」を明示し、PR reviewer（Step 4）にその旨を伝達すること。モックテストのみの結果を `PASS` と報告しないこと。


**SubAgent レート制限フォールバック:**

`test-runner` SubAgent がレート制限（エラーまたは空レスポンス）に達した場合、オーケストラレータは以下を実施する:

1. レート制限を検出し、オーケストラレータが直接 verify コマンドを実行する。
2. Verification Commands + `just check` をオーケストラレータ自身で実行し、結果を PR にコメントする。
3. LOOP_STATE に記録する。

**TEST_VERDICT: FAIL 時の処理（Step 2 完了直後）:**

Step 2 完了後に TEST_VERDICT が `FAIL` の場合は、adversarial-reviewer（Step 3）をスキップして Step 5（判定）へ進む。Step 5 でフィードバックをまとめ、Step 1 から再実行する。

#### Step 2.5: test-runner SubAgent role 逸脱チェック（オーケストラレータ）

Step 2 の test-runner SubAgent が完了した**直後**、オーケストラレータは以下のコマンドで role 逸脱（想定外コミット）を検出する。このチェックは Step 2 の結果を受け取った後、LOOP_STATE を tested phase に更新する前に実施すること。

```bash
# test-runner SubAgent が worktree に想定外 commit を追加していないか確認

# Step 1: fetch して origin 追跡 ref を最新化する
git -C "$expected_worktree_path" fetch origin "$expected_branch" 2>/dev/null

# Step 2a: ローカル未 push commit チェック（test-runner が commit したが未 push の場合）
UNEXPECTED_LOCAL=$(git -C "$expected_worktree_path" log "origin/$expected_branch..HEAD" --oneline 2>/dev/null)

# Step 2b: push 済み新規 commit チェック（test-runner が commit + push した場合）
# REVIEWED_HEAD_SHA は Step 3 pre-fetch で記録済みの承認済み PR head SHA
UNEXPECTED_PUSHED=$(git -C "$expected_worktree_path" log "${REVIEWED_HEAD_SHA}..origin/${expected_branch}" --oneline 2>/dev/null)

if [ -n "$UNEXPECTED_LOCAL" ] || [ -n "$UNEXPECTED_PUSHED" ]; then
  echo "[ROLE VIOLATION] test-runner SubAgent が worktree に commit を追加しました:"
  [ -n "$UNEXPECTED_LOCAL" ] && echo "  ローカル未 push: $UNEXPECTED_LOCAL"
  [ -n "$UNEXPECTED_PUSHED" ] && echo "  push 済み: $UNEXPECTED_PUSHED"
  # LOOP_STATE に記録して中断（下記手順を参照）
fi
```

**逸脱検出時の処理:**

1. `[ROLE VIOLATION]` として LOOP_STATE に記録する（`phase: role_violation`）:
   ```bash
   gh issue comment <Issue番号> --body "## LOOP_STATE
   \`\`\`yaml
   iteration: <N>
   phase: role_violation
   status: blocked
   pr_url: <PR_URL>
   last_verdict: null
   role_violation:
     detected_by: test-runner-completion-check
     unexpected_commits:
       - <commit hash>: <commit message>
     action_taken: pending  # revert または manual 実行後に actual_action フィールドを追記すること
   \`\`\`
   ## [ROLE VIOLATION] test-runner SubAgent が worktree にコミットを追加しました

   test-runner SubAgent の role 逸脱を検出しました。
   想定外コミット: <コミット一覧>

   **選択肢:**
   - 自動 revert: \`git -C $expected_worktree_path reset --hard origin/$expected_branch\` を実行する
   - 手動対応: オペレーターがコミット内容を確認して対処する"
   ```

2. Step 2 の TEST_VERDICT を強制的に `FAIL (role violation)` 扱いにする（コードの品質に関わらず、role 逸脱は即座にループ継続の阻止要因となる）。

3. 自動 revert または手動対応を選択する:
   - **自動 revert**（推奨）: `git -C "$expected_worktree_path" reset --hard origin/"$expected_branch"` を実行し、想定外コミットを除去する。
   - **手動対応**: オペレーターがコミット内容を確認し、revert するかどうかを判断する。
   - いずれの対応を実施した後も、LOOP_STATE の `role_violation.action_taken` フィールドを `revert` または `manual` に更新すること（初期値 `pending` のままにしない）。

4. 逸脱したコミットのハッシュ・メッセージを LOOP_STATE の `role_violation.unexpected_commits` フィールドに記録する。

> **背景**: PR #821（Issue #818）で test-runner SubAgent がコードを変更・commit・push した role 逸脱が発生した。PR #833 では委任プロンプトに「コード変更禁止」を明示したが、これは宣言的な制約のみで検出・中断に対応していなかった。このチェックにより自動検出・中断を実現する（Issue #836）。
